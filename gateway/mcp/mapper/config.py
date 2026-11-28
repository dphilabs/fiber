# fiber.gateway.mcp.mapper.config
import os
import re
import sys
import copy
import shlex
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

import orjson
from xphi.node.space.bind.resolver import resolve_path
from fiber.gateway.bridge.emitter import get_emitter

log = get_emitter("mcp.mapper.config")
ANCHOR_ROOT = Path(resolve_path("anchor")).resolve()

# Matches ${VAR}, ${VAR:-default}, and VSCode/Cursor-style ${env:VAR}
_ENV_PLACEHOLDER_RE = re.compile(
    r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


@dataclass(frozen=True)
class MappedMcpEntry:
    target_id: str
    exec_cmd: str
    env: Dict[str, str] = field(default_factory=dict)
    source_agent: str = "custom"
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def is_remote_url(self) -> bool:
        return self.exec_cmd.startswith(("http://", "https://"))


DEFAULT_MCP_MAPPER_RULES: Dict[str, Any] = {
    "root_keys": ["mcpServers", "servers", "context_servers"],
    "ignore_rules": {
        "targets": ["fiber-gateway", "fiber_gateway"],
        "cmd_substrings": ["/v1/mcp-gateway"],
    },
    "extraction_rules": {
        "url": ["url", "serverUrl", "uri", "httpUrl", "transport.url", "transport.uri"],
        "command": ["command.path", "command.command", "command", "transport.command"],
        "args": ["command.args", "args", "transport.args"],
        "env": ["command.env", "env", "transport.env"],
        "headers": [
            "headers",
            "transport.headers",
            "requestInit.headers",
            "transport.requestInit.headers",
        ],
        "disabled": ["disabled", "transport.disabled"],
    },
    "agent_profiles": {
        "fiber": {
            "markers": ["mcp.json", "anchor", ".anchor"],
            "paths": {"*": ["{anchor}/mcp.json"]},
        },
        "claude-code": {
            "markers": [".mcp.json", ".claude.json"],
            "paths": {"*": ["{cwd}/.mcp.json", "{home}/.claude.json"]},
        },
        "claude-desktop": {
            "markers": ["claude_desktop_config.json"],
            "paths": {
                "darwin": ["{home}/Library/Application Support/Claude/claude_desktop_config.json"],
                "win32": ["{appdata}/Claude/claude_desktop_config.json"],
                "linux": ["{home}/.config/Claude/claude_desktop_config.json"],
            },
        },
        "cursor": {
            "markers": [".cursor"],
            "paths": {"*": ["{cwd}/.cursor/mcp.json", "{home}/.cursor/mcp.json"]},
        },
        "windsurf": {
            "markers": ["windsurf", "mcp_config.json"],
            "paths": {"*": ["{home}/.codeium/windsurf/mcp_config.json"]},
        },
        "vscode": {
            "root_key": "servers",
            "markers": [".vscode"],
            "paths": {
                "darwin": ["{cwd}/.vscode/mcp.json", "{home}/Library/Application Support/Code/User/mcp.json"],
                "win32": ["{cwd}/.vscode/mcp.json", "{appdata}/Code/User/mcp.json"],
                "linux": ["{cwd}/.vscode/mcp.json", "{home}/.config/Code/User/mcp.json"],
            },
        },
        "zed": {
            "root_key": "context_servers",
            "markers": [".zed"],
            "paths": {
                "darwin": ["{cwd}/.zed/settings.json", "{home}/.config/zed/settings.json"],
                "win32": ["{cwd}/.zed/settings.json", "{appdata}/Zed/settings.json"],
                "linux": ["{cwd}/.zed/settings.json", "{home}/.config/zed/settings.json"],
            },
        },
    },
}


class McpConfigMapper:
    _rules: Optional[Dict[str, Any]] = None

    @classmethod
    def get_anchor_config_path(cls) -> Path:
        return ANCHOR_ROOT / "mcp.json"

    @classmethod
    def get_rules(cls) -> Dict[str, Any]:
        if cls._rules is not None:
            return cls._rules

        rules = copy.deepcopy(DEFAULT_MCP_MAPPER_RULES)
        custom_path = os.environ.get("FIBER_MCP_MAPPER_RULES_PATH")
        if custom_path:
            p = Path(custom_path)
            if p.is_file():
                try:
                    ext = orjson.loads(p.read_bytes())
                    if isinstance(ext, dict):
                        cls._deep_merge(rules, ext)
                        log.info(f"[ConfigMapper] Merged external mapper rules from {p}")
                except Exception as e:
                    log.warning(f"[ConfigMapper] Ignored invalid rules file {p}: {e}")
        cls._rules = rules
        return cls._rules

    @classmethod
    def reset_rules_cache(cls) -> None:
        cls._rules = None

    @classmethod
    def _deep_merge(cls, base: Dict[str, Any], override: Dict[str, Any]) -> None:
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                cls._deep_merge(base[k], v)
            else:
                base[k] = v

    @staticmethod
    def _traverse(data: Dict[str, Any], paths: List[str], expected_type: type) -> Any:
        for path_expr in paths:
            curr: Any = data
            for part in path_expr.split("."):
                if isinstance(curr, dict) and part in curr:
                    curr = curr[part]
                else:
                    curr = None
                    break
            if curr is not None and isinstance(curr, expected_type):
                return curr
        return None

    @staticmethod
    def _expand_env_placeholders(
        value: str, local_env: Optional[Dict[str, str]] = None
    ) -> str:
        if "${" not in value:
            return value

        def _replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            if local_env and var_name in local_env:
                return local_env[var_name]
            if var_name in os.environ:
                return os.environ[var_name]
            if default_val is not None:
                return default_val
            # Leave unresolved placeholder intact rather than silently blanking it out
            return match.group(0)

        return _ENV_PLACEHOLDER_RE.sub(_replacer, value)

    @classmethod
    def _clean_str_dict(
        cls,
        raw: Optional[Dict[str, Any]],
        *,
        expand_env: bool = False,
        local_env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        if not raw or not isinstance(raw, dict):
            return {}

        cleaned: Dict[str, str] = {}
        for k, v in raw.items():
            if v is None:
                continue
            key_str = str(k).strip()
            if not key_str:
                continue
            val_str = str(v)
            if expand_env:
                val_str = cls._expand_env_placeholders(val_str, local_env=local_env)
            cleaned[key_str] = val_str
        return cleaned

    @classmethod
    def _is_ignored(cls, target_id: str, exec_cmd: str) -> bool:
        ignore = cls.get_rules().get("ignore_rules", {})
        if target_id.lower() in {str(t).lower() for t in ignore.get("targets", [])}:
            return True
        return any(sub in exec_cmd for sub in ignore.get("cmd_substrings", []))

    @classmethod
    def _resolve_candidate_paths(cls) -> Dict[str, List[Path]]:
        rules = cls.get_rules()
        plat = "darwin" if sys.platform == "darwin" else ("win32" if sys.platform == "win32" else "linux")
        home = Path.home()
        tokens = {
            "anchor": str(ANCHOR_ROOT),
            "home": str(home),
            "cwd": str(Path.cwd()),
            "appdata": os.environ.get("APPDATA", str(home / "AppData" / "Roaming")),
        }

        resolved: Dict[str, List[Path]] = {}
        for agent, profile in rules.get("agent_profiles", {}).items():
            path_map = profile.get("paths", {})
            raw_list = path_map.get(plat, []) + path_map.get("*", [])
            resolved[agent] = [Path(tmpl.format(**tokens)) for tmpl in raw_list]
        return resolved

    @classmethod
    def discover_config_files(cls, agent: Optional[str] = None) -> List[Tuple[str, Path]]:
        candidates = cls._resolve_candidate_paths()
        target_agents = [agent.lower()] if agent else list(candidates.keys())
        seen_paths: set[Path] = set()
        found: List[Tuple[str, Path]] = []

        for ag in target_agents:
            for path in candidates.get(ag, []):
                if path.is_file():
                    resolved_p = path.resolve()
                    if resolved_p not in seen_paths:
                        seen_paths.add(resolved_p)
                        found.append((ag, path))
        return found

    @classmethod
    def parse_file(
        cls,
        config_path: Path,
        agent_hint: Optional[str] = None,
        *,
        expand_env: bool = True,
    ) -> List[MappedMcpEntry]:
        try:
            raw_data = orjson.loads(config_path.read_bytes())
        except Exception as e:
            log.error(f"[ConfigMapper] Failed to parse JSON from {config_path}: {e}")
            return []

        if not isinstance(raw_data, dict):
            return []

        source = agent_hint or cls._detect_agent_format(config_path, raw_data)
        servers_map = cls._extract_servers_map(raw_data)

        return [
            mapped
            for target_id, spec in servers_map.items()
            if isinstance(spec, dict)
            and (
                mapped := cls._normalize_entry(
                    str(target_id), spec, source, expand_env=expand_env
                )
            )
        ]

    @classmethod
    def scan_and_parse(
        cls,
        agent: Optional[str] = None,
        sync_to_anchor: bool = False,
        *,
        expand_env: bool = True,
    ) -> List[MappedMcpEntry]:
        merged: Dict[str, MappedMcpEntry] = {}
        raw_for_sync: Dict[str, MappedMcpEntry] = {}
        need_raw_sync = bool(sync_to_anchor and agent != "fiber")

        for ag_name, path in cls.discover_config_files(agent):
            for entry in cls.parse_file(path, agent_hint=ag_name, expand_env=expand_env):
                if entry.target_id not in merged:
                    merged[entry.target_id] = entry
                    log.debug(f"[ConfigMapper] Discovered [{entry.target_id}] from {ag_name} ({path})")

            if need_raw_sync:
                for raw_entry in cls.parse_file(path, agent_hint=ag_name, expand_env=False):
                    if raw_entry.target_id not in raw_for_sync:
                        raw_for_sync[raw_entry.target_id] = raw_entry

        results = list(merged.values())
        if need_raw_sync and raw_for_sync:
            cls.sync_entries_to_anchor(list(raw_for_sync.values()))
        return results

    @classmethod
    def sync_entries_to_anchor(cls, entries: List[MappedMcpEntry]) -> Optional[Path]:
        if not entries:
            return None

        anchor_path = cls.get_anchor_config_path()
        anchor_path.parent.mkdir(parents=True, exist_ok=True)

        data: Dict[str, Any] = {"mcpServers": {}}
        if anchor_path.is_file():
            try:
                loaded = orjson.loads(anchor_path.read_bytes())
                if isinstance(loaded, dict):
                    data = loaded
                    if not isinstance(data.get("mcpServers"), dict):
                        data["mcpServers"] = {}
            except Exception as e:
                log.warning(f"[ConfigMapper] Failed to read {anchor_path}, resetting structure: {e}")

        servers = data["mcpServers"]
        synced_count = 0

        for entry in entries:
            if cls._is_ignored(entry.target_id, entry.exec_cmd):
                continue

            existing_spec = servers.get(entry.target_id)
            spec_dict: Dict[str, Any] = (
                dict(existing_spec) if isinstance(existing_spec, dict) else {}
            )

            merged_env: Dict[str, str] = cls._clean_str_dict(spec_dict.get("env"))
            if entry.env:
                merged_env.update(cls._clean_str_dict(entry.env))

            merged_headers: Dict[str, str] = cls._clean_str_dict(spec_dict.get("headers"))
            if entry.headers:
                merged_headers.update(cls._clean_str_dict(entry.headers))

            if entry.is_remote_url:
                spec_dict.pop("command", None)
                spec_dict.pop("args", None)
                spec_dict["url"] = entry.exec_cmd
            else:
                spec_dict.pop("url", None)
                try:
                    parts = shlex.split(entry.exec_cmd)
                except ValueError:
                    parts = entry.exec_cmd.split()
                spec_dict["command"] = parts[0] if parts else entry.exec_cmd
                spec_dict["args"] = parts[1:] if len(parts) > 1 else []

            if merged_env:
                spec_dict["env"] = merged_env
            else:
                spec_dict.pop("env", None)

            if merged_headers:
                spec_dict["headers"] = merged_headers
            else:
                spec_dict.pop("headers", None)

            servers[entry.target_id] = spec_dict
            synced_count += 1

        if synced_count > 0:
            anchor_path.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
            log.info(f"[ConfigMapper] Synced {synced_count} target(s) -> {anchor_path}")
        return anchor_path

    @classmethod
    def _detect_agent_format(cls, path: Path, data: Dict[str, Any]) -> str:
        profiles = cls.get_rules().get("agent_profiles", {})
        path_tokens = {p.lower() for p in path.parts} | {path.name.lower()}

        for agent, prof in profiles.items():
            root_key = prof.get("root_key")
            if root_key and root_key in data:
                return agent
            if any(m.lower() in path_tokens for m in prof.get("markers", [])):
                return agent
        return "standard-mcp"

    @classmethod
    def _extract_servers_map(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        for key in cls.get_rules().get("root_keys", []):
            if isinstance(data.get(key), dict):
                return data[key]
        return data

    @classmethod
    def _normalize_entry(
        cls,
        target_id: str,
        spec: Dict[str, Any],
        source: str,
        *,
        expand_env: bool = True,
    ) -> Optional[MappedMcpEntry]:
        tid = target_id.strip()
        if not tid:
            return None

        ext = cls.get_rules()["extraction_rules"]

        if cls._traverse(spec, ext.get("disabled", ["disabled"]), bool) is True:
            return None

        raw_env = cls._traverse(spec, ext.get("env", ["env"]), dict)
        env_map = cls._clean_str_dict(raw_env, expand_env=expand_env)

        raw_headers = cls._traverse(spec, ext.get("headers", ["headers"]), dict)
        headers_map = cls._clean_str_dict(
            raw_headers, expand_env=expand_env, local_env=env_map
        )

        url_val = cls._traverse(spec, ext.get("url", ["url"]), str)
        cmd_bin = cls._traverse(spec, ext.get("command", ["command"]), str)

        if url_val and url_val.strip():
            raw_url = url_val.strip()
            exec_cmd = (
                cls._expand_env_placeholders(raw_url, local_env=env_map)
                if expand_env
                else raw_url
            )
        elif cmd_bin and cmd_bin.strip():
            raw_args = cls._traverse(spec, ext.get("args", ["args"]), list) or []
            norm_args = [
                cls._expand_env_placeholders(str(a), local_env=env_map)
                if expand_env
                else str(a)
                for a in raw_args
                if a is not None
            ]
            exec_cmd = shlex.join([cmd_bin.strip(), *norm_args])
        else:
            return None

        if cls._is_ignored(tid, exec_cmd):
            log.debug(f"[ConfigMapper] Skipped self-referential gateway entry: [{tid}]")
            return None

        return MappedMcpEntry(
            target_id=tid,
            exec_cmd=exec_cmd,
            env=env_map,
            source_agent=source,
            headers=headers_map,
        )
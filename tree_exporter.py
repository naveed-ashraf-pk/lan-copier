import base64
import datetime as _dt
import json
import os
import shlex
import socket
import stat
import tempfile
import time


def describe_local_host():
    host_name = socket.gethostname() or "localhost"
    host_ip = _best_ip(host_name)
    return _host_info(host_name, host_ip)


def suggest_export_filename(host_name, root_path, now=None):
    when = now or time.localtime()
    stamp = time.strftime("%Y%m%d-%H%M%S", when)
    folder = os.path.basename(os.path.normpath(root_path or "")) or "root"
    return f"{_safe_name(host_name)}-{_safe_name(folder)}-{stamp}.yaml"


def export_local_tree(out_path, panel, root_path, scope="all", max_depth=None,
                      depth_label="full", selected_paths=None):
    host_info = describe_local_host()
    entries = collect_local_entries(root_path, selected_paths, max_depth)
    doc = build_export_document(
        panel=panel, host_info=host_info, root_path=root_path, scope=scope,
        depth_label=depth_label, entries=entries)
    write_yaml_file(out_path, doc)
    return doc


def export_remote_tree(conn, out_path, panel, root_path, scope="all", max_depth=None,
                       depth_label="full", selected_paths=None):
    host_info, entries = collect_remote_entries(conn, root_path, selected_paths, max_depth)
    doc = build_export_document(
        panel=panel, host_info=host_info, root_path=root_path, scope=scope,
        depth_label=depth_label, entries=entries)
    write_yaml_file(out_path, doc)
    return doc


def collect_local_entries(root_path, selected_paths=None, max_depth=None):
    root_path = os.path.abspath(os.path.expanduser(root_path))
    if not os.path.isdir(root_path):
        raise OSError(f"not a directory: {root_path}")
    entries = {}
    starts = _local_start_paths(root_path, selected_paths)
    for path in starts:
        _walk_local(path, root_path, entries, max_depth)
    return {k: entries[k] for k in sorted(entries)}


def collect_remote_entries(conn, root_path, selected_paths=None, max_depth=None):
    payload = {
        "root_path": root_path,
        "selected_paths": sorted(selected_paths or []),
        "max_depth": max_depth,
        "host_hint": getattr(conn, "host", "") or "",
    }
    uname = _remote_uname(conn)
    data = None
    if uname == "Windows":
        data = _scan_remote_via_powershell(conn, payload)
        if data is None:
            data = _scan_remote_via_python(conn, payload)
    else:
        data = _scan_remote_via_python(conn, payload)
        if data is None:
            data = _scan_remote_via_unix_find(conn, payload, uname)
        if data is None:
            data = _scan_remote_via_powershell(conn, payload)
    if data is None:
        err = getattr(conn, "last_error", "") or "export scan failed"
        raise OSError(err)
    if data.get("error"):
        raise OSError(data["error"])
    host_name = data.get("host_name") or getattr(conn, "host", "remote")
    host_ip = data.get("host_ip") or getattr(conn, "host", "")
    cached = getattr(conn, "_export_host_info", None)
    if cached:
        host_name = cached.get("host_name") or host_name
        host_ip = cached.get("host_ip") or host_ip
    host_info = _host_info(host_name, host_ip)
    entries = {}
    for key, meta in (data.get("entries") or {}).items():
        if not key:
            continue
        entries[str(key)] = _normalize_entry(meta)
    return host_info, {k: entries[k] for k in sorted(entries)}


def describe_remote_host(conn):
    """Best-effort hostname + IP for a remote connection, cached on the
    connection so the UI shows a real machine name (e.g. "MacBook-Pro-…")
    instead of an IP in the suggested export file name."""
    cached = getattr(conn, "_export_host_info", None)
    if cached:
        return dict(cached)
    hint = getattr(conn, "host", "") or ""
    info = None
    if _remote_uname(conn) != "Windows":
        rc, out, err = _run_remote_posix(
            conn, "hostname 2>/dev/null || printf %s " + shlex.quote(hint or "remote"), timeout=15)
        host_name = (out or "").strip() or hint or "remote"
        info = _host_info(host_name, hint)
    if info is None:
        info = _host_info(hint or "remote", hint)
    try:
        conn._export_host_info = dict(info)
    except Exception:
        pass
    return info


def _remote_uname(conn):
    """Remote OS, cached. Real connections expose the unified lazy `os_type`
    cache (transport-owned, single source of truth); duck-typed test doubles
    fall back to the historical exporter-owned detection + `_export_uname`."""
    if getattr(conn, "os_type", None):
        try:
            conn._export_uname = conn.os_type
        except Exception:
            pass
        return conn.os_type
    cached = getattr(conn, "_export_uname", None)
    if cached:
        return cached
    uname = ""
    try:
        rc, out, err = _run_remote_posix(conn, "uname -s", timeout=15)
        uname = (out or "").strip() if rc == 0 else ""
    except Exception:
        pass
    if not uname:
        # uname failing usually means the remote default shell is cmd.exe
        # (Windows OpenSSH); treat it as Windows so PowerShell is tried.
        uname = "Windows"
    try:
        conn._export_uname = uname
    except Exception:
        pass
    return uname


def build_export_document(panel, host_info, root_path, scope, depth_label, entries):
    root_path = os.path.expanduser(root_path)
    total_files = sum(1 for meta in entries.values() if meta.get("type") == "file")
    total_directories = sum(1 for meta in entries.values() if meta.get("type") == "directory")
    total_size_bytes = sum(int(meta.get("size_bytes", 0) or 0) for meta in entries.values())
    meta = {
        "panel": panel,
        "host_name": host_info.get("host_name") or "unknown",
        "host_ip": host_info.get("host_ip") or "",
        "host_display": host_info.get("host_display") or host_info.get("host_name") or "unknown",
        "root_path": root_path,
        "exported_at": _fmt_ts(time.time()),
        "scope": scope,
        "depth": depth_label,
        "total_entries": len(entries),
        "total_files": total_files,
        "total_directories": total_directories,
        "total_size_bytes": total_size_bytes,
    }
    return {"meta": meta, "entries": {k: entries[k] for k in sorted(entries)}}


def dump_yaml_document(doc):
    lines = ["meta:"]
    for key in (
            "panel", "host_name", "host_ip", "host_display", "root_path",
            "exported_at", "scope", "depth", "total_entries", "total_files",
            "total_directories", "total_size_bytes"):
        if key not in doc["meta"]:
            continue
        lines.append(f"  {key}: {_yaml_scalar(doc['meta'][key])}")
    lines.append("")
    lines.append("entries:")
    if not doc["entries"]:
        lines.append("  {}")
        return "\n".join(lines) + "\n"
    for path in sorted(doc["entries"]):
        lines.append(f"  {_yaml_scalar(path)}:")
        meta = doc["entries"][path]
        for key in ("type", "size_bytes", "modified", "created", "mode", "symlink_target"):
            if key in meta and meta[key] not in (None, ""):
                lines.append(f"    {key}: {_yaml_scalar(meta[key])}")
    return "\n".join(lines) + "\n"


def write_yaml_file(out_path, doc):
    out_path = os.path.abspath(os.path.expanduser(out_path))
    parent = os.path.dirname(out_path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tree-export-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump_yaml_document(doc))
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _local_start_paths(root_path, selected_paths):
    if selected_paths:
        pruned = _prune_selected_paths(root_path, selected_paths)
        return [path for path in pruned if os.path.lexists(path)]
    try:
        names = sorted(os.listdir(root_path), key=str.lower)
    except OSError:
        names = sorted(os.listdir(root_path))
    return [os.path.join(root_path, name) for name in names]


def _walk_local(path, root_path, entries, max_depth):
    try:
        st = os.lstat(path)
    except OSError:
        return
    rel = os.path.relpath(path, root_path)
    if rel == ".":
        return
    rel = rel.replace(os.sep, "/")
    is_link = stat.S_ISLNK(st.st_mode)
    is_dir = stat.S_ISDIR(st.st_mode)
    kind = _entry_type(is_dir, is_link)
    key = rel.rstrip("/") + "/" if kind == "directory" else rel
    depth = _entry_depth(key)
    if max_depth is not None and depth > max_depth:
        return
    meta = _entry_from_stat(st, kind, path)
    prev = entries.get(key)
    if prev is None or prev == meta:
        entries[key] = meta
    if kind != "directory" or is_link:
        return
    if max_depth is not None and depth >= max_depth:
        return
    try:
        with os.scandir(path) as it:
            children = sorted(list(it), key=lambda e: e.name.lower())
    except OSError:
        return
    for child in children:
        _walk_local(child.path, root_path, entries, max_depth)


def _entry_from_stat(st, kind, path=None):
    meta = {
        "type": kind,
        "modified": _fmt_ts(st.st_mtime),
        "created": _fmt_ts(_created_ts(st)),
    }
    mode = stat.S_IMODE(st.st_mode)
    if mode:
        meta["mode"] = f"0{mode:o}"
    if kind != "directory":
        meta["size_bytes"] = int(getattr(st, "st_size", 0) or 0)
    if kind == "symlink" and path is not None:
        try:
            meta["symlink_target"] = os.readlink(path)
        except OSError:
            pass
    return meta


def _normalize_entry(meta):
    out = {"type": meta.get("type") or "other"}
    if meta.get("size_bytes") not in (None, ""):
        out["size_bytes"] = int(meta["size_bytes"])
    for key in ("modified", "created", "mode", "symlink_target"):
        if meta.get(key) not in (None, ""):
            out[key] = str(meta[key])
    return out


def _entry_type(is_dir, is_link):
    if is_link:
        return "symlink"
    if is_dir:
        return "directory"
    return "file"


def _created_ts(st):
    btime = getattr(st, "st_birthtime", None)
    if btime not in (None, 0):
        return btime
    return getattr(st, "st_ctime", 0) or 0


def _fmt_ts(value):
    if value in (None, ""):
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, TypeError, ValueError):
        return ""


def _best_ip(host_hint):
    candidates = []
    try:
        candidates.extend(socket.gethostbyname_ex(host_hint)[2])
    except Exception:
        pass
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host_hint, None, socket.AF_INET):
            candidates.append(sockaddr[0])
    except Exception:
        pass
    seen = []
    for ip in candidates:
        if ip not in seen:
            seen.append(ip)
    for ip in seen:
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            return ip
    return ""


def _host_info(host_name, host_ip):
    host_name = host_name or "unknown"
    host_ip = host_ip or ""
    host_display = f"{host_name} ({host_ip})" if host_ip else host_name
    return {"host_name": host_name, "host_ip": host_ip, "host_display": host_display}


def _safe_name(text):
    text = (text or "unknown").strip()
    out = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("-")
    cleaned = "".join(out).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "unknown"


def _prune_selected_paths(root_path, selected_paths):
    root_path = os.path.abspath(os.path.expanduser(root_path))
    cleaned = []
    for path in selected_paths:
        if not path:
            continue
        full = os.path.abspath(os.path.expanduser(path))
        try:
            if os.path.commonpath([root_path, full]) != root_path:
                continue
        except ValueError:
            continue
        cleaned.append(full)
    cleaned = sorted(set(cleaned), key=lambda p: (len(p), p.lower()))
    out = []
    for path in cleaned:
        if any(path == parent or path.startswith(parent.rstrip(os.sep) + os.sep) for parent in out):
            continue
        out.append(path)
    return out


def _entry_depth(key):
    return key.rstrip("/").count("/") + 1


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps("" if value is None else str(value), ensure_ascii=True)


# Remote worker script. Kept as clean multi-line Python 3 and carried to the
# host as a base64 blob via a single-statement bootstrap so no remote shell
# quoting, newline mangling, or "def after semicolon" can break it.
PYTHON_SCANNER = r'''
import json
import os
import socket
import stat
import sys

cfg = json.loads(sys.argv[2])
root = cfg["root_path"]
selected = cfg.get("selected_paths") or []
max_depth = cfg.get("max_depth")
host_hint = cfg.get("host_hint") or ""


def fmt_ts(value):
    try:
        import datetime
        return datetime.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def best_ip(host):
    ips = []
    try:
        ips.extend(socket.gethostbyname_ex(host)[2])
    except Exception:
        pass
    for ip in ips:
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            return ip
    return ips[0] if ips else host_hint


def created_ts(st):
    return getattr(st, "st_birthtime", None) or getattr(st, "st_ctime", 0) or 0


entries = {}


def add(path):
    try:
        st = os.lstat(path)
    except OSError:
        return
    rel = os.path.relpath(path, root)
    if rel == ".":
        return
    rel = rel.replace("\\", "/")
    is_link = stat.S_ISLNK(st.st_mode)
    is_dir = stat.S_ISDIR(st.st_mode)
    kind = "symlink" if is_link else ("directory" if is_dir else "file")
    key = rel.rstrip("/") + "/" if kind == "directory" else rel
    depth = key.rstrip("/").count("/") + 1
    if max_depth is not None and depth > max_depth:
        return
    meta = {"type": kind, "modified": fmt_ts(st.st_mtime), "created": fmt_ts(created_ts(st))}
    mode = stat.S_IMODE(st.st_mode)
    if mode:
        meta["mode"] = "0%o" % mode
    if kind != "directory":
        meta["size_bytes"] = int(getattr(st, "st_size", 0) or 0)
    if kind == "symlink":
        try:
            meta["symlink_target"] = os.readlink(path)
        except OSError:
            pass
    entries[key] = meta
    if kind != "directory" or is_link:
        return
    if max_depth is not None and depth >= max_depth:
        return
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except Exception:
        try:
            names = sorted(os.listdir(path))
        except Exception:
            return
    for name in names:
        add(os.path.join(path, name))


try:
    if selected:
        starts = selected
    else:
        starts = [os.path.join(root, n) for n in sorted(os.listdir(root), key=str.lower)]
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
    sys.exit(0)

for p in starts:
    add(p)

host_name = socket.gethostname() or host_hint or "remote"
print(json.dumps({"host_name": host_name, "host_ip": best_ip(host_name), "entries": entries}, sort_keys=True))
'''


def _scan_remote_via_python(conn, payload):
    code_b64 = base64.b64encode(PYTHON_SCANNER.encode("utf-8")).decode("ascii")
    arg = json.dumps(payload, separators=(",", ":"))
    # Single simple statement: no compound statements in the -c string.
    bootstrap = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
    for exe in ("python3", "python"):
        cmd = f"{exe} -c {shlex.quote(bootstrap)} {shlex.quote(code_b64)} {shlex.quote(arg)}"
        rc, out, err = _run_remote_posix(conn, cmd, timeout=120)
        if rc == 0 and out.strip():
            try:
                return json.loads(out)
            except ValueError:
                pass
        if rc == 0:
            continue
        if err:
            conn.last_error = _clean_remote_err(err)
    return None


def _scan_remote_via_powershell(conn, payload):
    payload_b64 = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    script = f"""
$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload_b64}'))
$cfg = $json | ConvertFrom-Json
$root = [string]$cfg.root_path
$selected = @($cfg.selected_paths)
$maxDepth = $cfg.max_depth
$hostHint = [string]$cfg.host_hint
function FmtTs([datetime]$dt) {{
  if (-not $dt) {{ return '' }}
  return $dt.ToString('yyyy-MM-dd HH:mm:ss')
}}
function AddEntry([hashtable]$map, [string]$key, $item, [bool]$isLink) {{
  $type = if ($isLink) {{ 'symlink' }} elseif ($item.PSIsContainer) {{ 'directory' }} else {{ 'file' }}
  $meta = @{{
    type = $type
    modified = FmtTs $item.LastWriteTime
    created = FmtTs $item.CreationTime
  }}
  if (-not $item.PSIsContainer) {{ $meta.size_bytes = [int64]$item.Length }}
  if ($item.Mode -and -not $item.PSIsContainer) {{ $meta.mode = [string]$item.Mode }}
  if ($isLink -and $item.Target) {{ $meta.symlink_target = [string]($item.Target -join ', ') }}
  $map[$key] = $meta
}}
function WalkPath([string]$path, [hashtable]$entries) {{
  try {{ $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop }} catch {{ return }}
  $rel = [IO.Path]::GetRelativePath($root, $path).Replace('\\', '/')
  if ($rel -eq '.') {{ return }}
  $isLink = (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
  $key = if ($item.PSIsContainer) {{ $rel.TrimEnd('/') + '/' }} else {{ $rel }}
  $depth = ($key.TrimEnd('/') -split '/').Count
  if ($maxDepth -ne $null -and $depth -gt [int]$maxDepth) {{ return }}
  AddEntry $entries $key $item $isLink
  if (-not $item.PSIsContainer -or $isLink) {{ return }}
  if ($maxDepth -ne $null -and $depth -ge [int]$maxDepth) {{ return }}
  try {{
    $children = Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop | Sort-Object Name
  }} catch {{ return }}
  foreach ($child in $children) {{ WalkPath $child.FullName $entries }}
}}
try {{
  if ($selected.Count -gt 0) {{
    $starts = $selected
  }} else {{
    $starts = Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop | Sort-Object Name | ForEach-Object FullName
  }}
}} catch {{
  @{{ error = $_.Exception.Message }} | ConvertTo-Json -Compress
  exit 0
}}
$entries = @{{}}
foreach ($start in $starts) {{ WalkPath ([string]$start) $entries }}
$hostName = $env:COMPUTERNAME
if (-not $hostName) {{ $hostName = [System.Net.Dns]::GetHostName() }}
$ips = @([System.Net.Dns]::GetHostAddresses($hostName) | Where-Object {{ $_.AddressFamily -eq 'InterNetwork' }} | ForEach-Object IPAddressToString)
$hostIp = if ($ips.Count -gt 0) {{ $ips[0] }} else {{ $hostHint }}
@{{ host_name = $hostName; host_ip = $hostIp; entries = $entries }} | ConvertTo-Json -Depth 8 -Compress
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    rc, out, err = conn._run_cmd(["ssh"] + conn._opts() + [conn.target, f"powershell -NoProfile -EncodedCommand {encoded}"], timeout=120)
    if rc == 0 and out.strip():
        try:
            return json.loads(out)
        except ValueError:
            pass
    if rc != 0 and err:
        conn.last_error = _clean_remote_err(err)
    return None


def _scan_remote_via_unix_find(conn, payload, uname):
    """Native find/stat fallback used when the remote host has no Python.

    Linux/GNU find uses -printf; macOS (Darwin) uses BSD find + `stat -f`.
    Field order in the emitted lines is kept identical so one parser works:
    <kind or type> <mode> <size> <mtime> <created> <path> [<link>]"""
    root = payload["root_path"]
    selected = payload.get("selected_paths") or []
    max_depth = payload.get("max_depth")
    if selected:
        quoted = " ".join(shlex.quote(p) for p in selected)
        depth_bits = ["-mindepth 0"]
        if max_depth is not None:
            depth_bits.append(f"-maxdepth {int(max_depth) - 1}")
    else:
        quoted = shlex.quote(root)
        depth_bits = ["-mindepth 1"]
        if max_depth is not None:
            depth_bits.append(f"-maxdepth {int(max_depth)}")
    depth_sql = " ".join(depth_bits)
    if uname == "Darwin":
        fmt = "%N\t%HT\t%p\t%z\t%m\t%B\t%Y"  # literal tabs: BSD stat -f
        find_cmd = (f"find {quoted} {depth_sql} \\( -type d -o -type f -o -type l \\) "
                    f"-exec stat -f '{fmt}' \\{{}} +")
    else:
        find_cmd = (f"find {quoted} {depth_sql} \\( -type d -o -type f -o -type l \\) "
                    f"-printf '%y\\t%m\\t%s\\t%T@\\t%C@\\t%p\\t%l\\n'")
    cmd = ("LC_ALL=C ROOT=" + shlex.quote(root) + " ; "
           "HOST=$(hostname 2>/dev/null || printf remote) ; " + find_cmd)
    rc, out, err = _run_remote_posix(conn, cmd, timeout=120)
    if rc != 0:
        if err:
            conn.last_error = _clean_remote_err(err)
        return None
    entries = {}
    for line in out.splitlines():
        if uname == "Darwin":
            # stat -f '%N\t%HT\t%p\t%z\t%m\t%B\t%Y'
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            full, type_s, mode_s, size_s, mtime_s, birth_s = parts[:6]
            link = parts[6] if len(parts) > 6 else ""
            kind = {"Directory": "directory", "Regular File": "file",
                    "Symbolic Link": "symlink"}.get(type_s, "other")
            mtime = _epoch_or_float(mtime_s)
            ctime = _epoch_or_float(birth_s)
        else:
            # GNU find -printf '%y\t%m\t%s\t%T@\t%C@\t%p\t%l'
            parts = line.split("\t", 6)
            if len(parts) < 6:
                continue
            kind_code, mode_s, size_s, mtime_s, ctime_s, full = parts[:6]
            link = parts[6] if len(parts) > 6 else ""
            kind = {"d": "directory", "f": "file", "l": "symlink"}.get(kind_code, "other")
            mtime = mtime_s
            ctime = ctime_s
        rel = _remote_relpath(root, full)
        if not rel:
            continue
        key = rel.rstrip("/") + "/" if kind == "directory" else rel
        meta = {"type": kind, "modified": _fmt_ts(mtime), "created": _fmt_ts(ctime)}
        mode_s = str(mode_s or "").strip()
        if mode_s.isdigit():
            try:
                perms = int(mode_s, 8) & 0o7777
                meta["mode"] = f"0{perms:o}"
            except ValueError:
                pass
        if kind != "directory":
            try:
                meta["size_bytes"] = int(size_s)
            except ValueError:
                meta["size_bytes"] = 0
        if kind == "symlink" and link:
            meta["symlink_target"] = link
        entries[key] = meta
    return {
        "host_name": getattr(conn, "host", "remote"),
        "host_ip": getattr(conn, "host", ""),
        "entries": entries,
    }


def _epoch_or_float(value):
    s = str(value or "").strip()
    try:
        return float(s)
    except ValueError:
        return ""


def _run_remote_posix(conn, remote_cmd, timeout):
    return conn._run_cmd(["ssh"] + conn._opts() + [conn.target, remote_cmd], timeout=timeout)


def _remote_relpath(root, full):
    root = (root or "").replace("\\", "/").rstrip("/")
    full = (full or "").replace("\\", "/")
    if not root:
        return full.lstrip("/")
    if full == root:
        return ""
    if full.startswith(root + "/"):
        return full[len(root) + 1:]
    return full.lstrip("/")


def _clean_remote_err(err):
    return str(err or "").strip().replace("\r", "").replace("\n", " | ")[:300]

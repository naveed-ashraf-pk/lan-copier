"""Pure builders for Windows PowerShell remote commands.

Windows OpenSSH does not give us a POSIX shell, and cmd.exe quoting is
deliberately avoided: every script is transported as a UTF-16LE base64 blob
via `powershell -NoProfile -EncodedCommand <b64>`, which makes the whole
script "one token" to cmd and immune to path quoting bugs (the same approach
the tree exporter already uses for Windows hosts).

Every function here returns the **full remote command string** (the fragment
placed after `ssh <opts> <target>`) by embedding the given paths inside the
encoded script with escaping, so a user path can never break out of the
string it appears in.  None of these functions perform I/O; each has an
exact-output / round-trip unit test in tests.py.
"""

import base64

_POWERSHELL = "powershell -NoProfile -EncodedCommand "


def _ps_str(path):
    """Escape a path for embedding inside a PowerShell single-quoted string
    (single quotes are the safe choice: no variable/escape expansion)."""
    return str(path).replace("'", "''")


def _script(body):
    encoded = base64.b64encode(body.encode("utf-16le")).decode("ascii")
    return _POWERSHELL + encoded


def exists(path):
    body = ("if (Test-Path -LiteralPath '" + _ps_str(path)
            + "') { exit 0 } else { exit 1 }")
    return _script(body)


def list_dir(path):
    """Children as TSV lines: ISDIR<TAB>ISLINK<TAB>SIZE<TAB>EPOCH<TAB>NAME.
    ISDIR/ISLINK are 1/0; EPOCH is the mtime as a unix timestamp."""
    body = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
        "function Emit($it) {\n"
        "  $isLink = 0; $isDir = 0\n"
        "  if (($it.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { $isLink = 1 }\n"
        "  elseif ($it.PSIsContainer) { $isDir = 1 }\n"
        "  $epoch = [DateTimeOffset]::new($it.LastWriteTime).ToUnixTimeSeconds()\n"
        "  [Console]::Out.WriteLine(\"$isDir`t$isLink`t$($it.Length)`t$epoch`t$($it.Name)\")\n"
        "}\n"
        "try {\n"
        "  Get-ChildItem -LiteralPath '" + _ps_str(path) + "' -Force | ForEach-Object { Emit $_ }\n"
        "} catch {\n"
        "  [Console]::Error.WriteLine($_.Exception.Message)\n"
        "  exit 1\n"
        "}"
    )
    return _script(body)


def stat_bytes_files(path):
    """Print a single line `BYTES FILES` for the path (recursive for dirs)."""
    body = (
        "$p = '" + _ps_str(path) + "'\n"
        "if (Test-Path -LiteralPath $p -PathType Container) {\n"
        "  $r = @(Get-ChildItem -LiteralPath $p -Recurse -File -Force -ErrorAction SilentlyContinue)\n"
        "  $b = 0; foreach ($f in $r) { $b += $f.Length }\n"
        "  [Console]::Out.WriteLine(\"$b $($r.Count)\")\n"
        "} else {\n"
        "  $it = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue\n"
        "  if ($null -eq $it) { exit 0 }\n"
        "  [Console]::Out.WriteLine(\"$($it.Length) 1\")\n"
        "}"
    )
    return _script(body)


def tree(root):
    """Every file under root as `REL<T>SIZE` lines; REL is forward-slashed and
    relative to root. Empty folders and reparse-point links are not listed."""
    body = (
        "$base = '" + _ps_str(root) + "'.Replace('\\', '/').TrimEnd('/')\n"
        "Get-ChildItem -LiteralPath '" + _ps_str(root)
        + "' -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {\n"
        "  $f = $_.FullName.Replace('\\', '/')\n"
        "  if ($f.StartsWith($base + '/')) { $f = $f.Substring($base.Length + 1) }\n"
        "  [Console]::Out.WriteLine(\"$f`t$($_.Length)\")\n"
        "}"
    )
    return _script(body)


def delete_recurse(path):
    """Remove a file/dir/symlink; retries 5x300ms on transient locks."""
    body = (
        "$p = '" + _ps_str(path) + "'\n"
        "$n = 0\n"
        "while ($true) {\n"
        "  try {\n"
        "    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop\n"
        "    exit 0\n"
        "  } catch {\n"
        "    $n++\n"
        "    if ($n -ge 5) { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n"
        "    Start-Sleep -Milliseconds 300\n"
        "  }\n"
        "}"
    )
    return _script(body)


def mkdir_p(path):
    body = ("New-Item -ItemType Directory -Path '" + _ps_str(path)
            + "' -Force | Out-Null")
    return _script(body)


def rename(src, dst):
    """Move/rename a path; kept as a plain cmdlet (throw/fail leaves the
    destination untouched, which is what the move fast-path fallback needs)."""
    body = ("Move-Item -LiteralPath '" + _ps_str(src) + "' -Destination '"
            + _ps_str(dst) + "' -Force -ErrorAction Stop")
    return _script(body)


def unique_path(path):
    body = (
        "$cand = '" + _ps_str(path) + "'\n"
        "if (-not (Test-Path -LiteralPath $cand)) { [Console]::Out.WriteLine($cand); exit 0 }\n"
        "$di = [IO.Path]::GetDirectoryName($cand)\n"
        "$name = [IO.Path]::GetFileNameWithoutExtension($cand)\n"
        "$ext = [IO.Path]::GetExtension($cand)\n"
        "$i = 1\n"
        "while ($true) {\n"
        "  $c = Join-Path $di ('{0} ({1}){2}' -f $name, $i, $ext)\n"
        "  if (-not (Test-Path -LiteralPath $c)) { [Console]::Out.WriteLine($c); exit 0 }\n"
        "  $i++\n"
        "}"
    )
    return _script(body)


def home_dir():
    return _script("[Console]::Out.WriteLine($env:USERPROFILE)")


def hostname():
    return _script("[Console]::Out.WriteLine($env:COMPUTERNAME)")


def has_tar():
    return _script("if (Get-Command tar.exe -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }")


def tar_read(parent, name):
    """Stream <name> under <parent> as a plain tar archive on stdout."""
    body = (
        "tar.exe -C '" + _ps_str(parent) + "' -cf - -- '" + _ps_str(name) + "'\n"
        "exit $LASTEXITCODE"
    )
    return _script(body)


def tar_extract(part):
    """Stream-extract a plain tar archive from stdin into an existing <part>."""
    body = (
        "tar.exe -C '" + _ps_str(part) + "' -xpf -\n"
        "exit $LASTEXITCODE"
    )
    return _script(body)


def merge_into(src, dst):
    """Per-entry merge of <src> into <dst> (both directories). Files
    overwrite same-named files, dirs recurse, reparse-point links are
    copied as links. Nothing in <dst> not being overwritten is touched."""
    body = (
        "function Merge($s, $d) {\n"
        "  Get-ChildItem -LiteralPath $s -Force -ErrorAction SilentlyContinue | ForEach-Object {\n"
        "    $n = $_.Name\n"
        "    $t = Join-Path $d $n\n"
        "    $isLink = (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)\n"
        "    if ($isLink) {\n"
        "      if (Test-Path -LiteralPath $t) { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }\n"
        "      Copy-Item -LiteralPath $_.FullName -Destination $t -ErrorAction SilentlyContinue\n"
        "    } elseif ($_.PSIsContainer) {\n"
        "      if (Test-Path -LiteralPath $t -PathType Container) {\n"
        "        MergeInto $_.FullName $t\n"
        "      } else {\n"
        "        if (Test-Path -LiteralPath $t) { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }\n"
        "        Copy-Item -LiteralPath $_.FullName -Destination $t -Recurse -Force\n"
        "      }\n"
        "    } else {\n"
        "      if (Test-Path -LiteralPath $t) { Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue }\n"
        "      Copy-Item -LiteralPath $_.FullName -Destination $t -Force -ErrorAction Stop\n"
        "    }\n"
        "  }\n"
        "}\n"
        "MergeInto '" + _ps_str(src) + "' '" + _ps_str(dst) + "'"
    )
    return _script(body)

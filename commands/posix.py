"""Pure builders for POSIX (Linux/macOS) remote command strings.

Every function in this module returns one of two shapes:
  - a *remote command string*: the fragment placed after `ssh <opts> <target>`
    on the command line, quoted with shlex.quote so a user-supplied path can
    never inject shell metacharacters, or
  - a local *argv list* for a subprocess that runs on the control machine
    (e.g. the local `tar` reader used to stream into a remote destination).

None of these functions perform I/O.  Each has an exact-output unit test in
tests.py; changing a command string here must be accompanied by the matching
test change so the escaping contract is always pinned.
"""

import shlex


def q(path):
    """POSIX single-token quote for a path (never interpolated raw)."""
    return shlex.quote(str(path))


def uname():
    """Remote OS probe. A failed/empty result is treated as Windows later."""
    return "uname -s"


def rm_rf(path):
    """Force-remove a file, folder, or symlink remotely. A trailing slash is
    never passed, so `rm` unlinks a symlink instead of traversing it."""
    return f"rm -rf -- {q(path)}"


def mv(src, dst):
    """Rename (or move) a remote path; same filesystem = atomic rename."""
    return f"mv -- {q(src)} {q(dst)}"


def mkdir_p(path):
    return f"mkdir -p -- {q(path)}"


def test_exists(path):
    return f"test -e {q(path)}"


def ls_la(path):
    return f"LC_ALL=C ls -la -- {q(path)}"


def find_stat_gnu(path):
    """Print '<bytes> <files>' using GNU find; `-printf` needs GNU findutils."""
    return (f"find {q(path)} -type f -printf '%s\\n' 2>/dev/null | "
            f"awk '{{s+=$1; n++}} END {{print s+0, n+0}}'")


def find_stat_darwin(path):
    """Print '<bytes> <files>' using BSD find + stat (macOS native)."""
    return (f"find {q(path)} -type f -exec stat -f '%z' {{}} + 2>/dev/null | "
            f"awk '{{s+=$1; n++}} END {{print s+0, n+0}}'")


def find_tree_gnu(path):
    """Recursive '<path> <size>' listing for the tree walk (GNU find)."""
    return f"find {q(path)} -type f -printf '%p %s\\n'"


def find_tree_darwin(path):
    """Recursive '<path> <size>' listing for the tree walk (macOS find)."""
    return f"find {q(path)} -type f -exec stat -f '%N %z' {{}} +"


def tar_read_remote(parent, name):
    """Stream a folder/file as a tar archive on a remote host. `parent` is the
    directory containing `name`; entries are rooted at `name` (no absolute
    paths, so a stripped/targeted extraction is always safe)."""
    return f"LC_ALL=C tar -C {q(parent)} -cf - -- {q(name)}"


def tar_read_local(parent, name):
    """argv for a local `tar` creation stream (control machine)."""
    return ["tar", "-C", parent, "-cf", "-", "--", name]


def tar_extract_remote(part):
    """Stream-extract a tar archive into an existing remote `part` dir. The
    archive is rooted at the source basename, so `part` gains exactly one
    top-level entry; placement then renames/merges it onto the final name."""
    return f"tar -C {q(part)} -xpf -"


def tar_extract_local(part):
    """argv for the local `tar` extractor; strips the leading component so
    the part dir holds the item's direct children (matches existing local
    folder-transfer semantics)."""
    return ["tar", "-C", part, "--strip-components=1", "-xpf", "-"]


def merge_dir_script():
    """A portable POSIX `sh` script that merges the *contents* of a source
    dir into a target dir entry-by-entry: files overwrite same-named files
    (atomic via temp+mv), dirs recurse, symlinks are recreated as symlinks
    (never followed), and nothing in the target that is not being overwritten
    is touched.  Invoked as `sh -c SCRIPT _ <src> <dst>` (so $1/$2 are the
    dirs) — the script body goes through shlex.quote, escaping is centralised.

    Known limitation: file names containing a newline are skipped (mirrors the
    shell ecosystem's general inability to address them through globs)."""
    return """
_p() { local it n
  it=$1; d=$2; n=${it##*/}
  if [ -L "$it" ]; then
    rm -rf -- "$d/$n" && ln -s -- "$(readlink -- "$it")" "$d/$n"
  elif [ -d "$it" ]; then
    if [ -d "$d/$n" ] && [ ! -L "$d/$n" ]; then
      merge "$it" "$d/$n"
    else
      rm -rf -- "$d/$n" && cp -a -- "$it" "$d/$n"
    fi
  else
    rm -rf -- "$d/$n" && cp -p -- "$it" "$d/$n"
  fi
}
merge() { local it
  for it in "$1"/*; do
    [ -e "$it" ] || [ -L "$it" ] || continue
    _p "$it" "$2"
  done
  for it in "$1"/.[!.]* "$1"/..?*; do
    [ -e "$it" ] || [ -L "$it" ] || continue
    _p "$it" "$2"
  done
}
merge "$1" "$2"
"""
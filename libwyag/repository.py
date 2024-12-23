import collections
import configparser
from datetime import datetime
import grp
import pwd
from fnmatch import fnmatch
import hashlib
from math import ceil
import os
import re
import sys
import zlib
import io
import typing
import argparse

WYAG_DIR = ".wyag"


def repo_path(repo: typing.ForwardRef("GitRepository"), *path) -> str:
    """Compute path under repo's git_dir."""
    return os.path.join(repo.git_dir, *path)


def repo_dir(
    repo: typing.ForwardRef("GitRepository"), *path, mkdir: bool = False
) -> str | None:
    """Same as repo_path, but mkdir *path if absent if mkdir."""

    path = repo_path(repo, *path)

    if os.path.exists(path):
        if os.path.isdir(path):
            return path
        else:
            raise NotADirectoryError("Not a directory {path}")

    if mkdir:
        os.makedirs(path)
        return path
    else:
        return None


def repo_file(
    repo: typing.ForwardRef("GitRepository"), *path, mkdir: bool = False
) -> str:
    """
    Same as repo_path, but create dirname(*path) if absent.
    For example, repo_file(r, \"refs\", \"remotes\", \"origin\", \"HEAD\") will create
    .git/refs/remotes/origin.
    """

    if repo_dir(repo, *path[:-1], mkdir=mkdir):
        return repo_path(repo, *path)


class GitRepository:
    """A git repository"""

    def __init__(self, path: str, force: bool = False):
        self.work_tree: str = path
        self.git_dir: str = os.path.join(path, WYAG_DIR)

        if not (force or os.path.isdir(self.git_dir)):
            raise RuntimeError(f"Not a Git repository {path}")

        # Read configuration file in .git/config
        self.conf = configparser.ConfigParser()
        cf = repo_file(self, "config")

        if cf and os.path.exists(cf):
            self.conf.read([cf])
        elif not force:
            raise FileNotFoundError("Configuration file missing")

        if not force:
            vers = int(self.conf.get("core", "repositoryformatversion"))
            if vers != 0:
                raise RuntimeError(f"Unsupported repositoryformatversion {vers}")


def repo_create(path: str) -> GitRepository:
    """Create a new repository at path."""

    repo = GitRepository(path, True)

    # First, we make sure the path either doesn't exist or is an
    # empty dir.
    if os.path.exists(repo.work_tree):
        if not os.path.isdir(repo.work_tree):
            raise RuntimeError(f"{path} is not a directory!")
        if os.path.exists(repo.git_dir) and os.listdir(repo.git_dir):
            raise RuntimeError(f"{path} is not empty!")
    else:
        os.makedirs(repo.work_tree)

    assert repo_dir(repo, "branches", mkdir=True)
    assert repo_dir(repo, "objects", mkdir=True)
    assert repo_dir(repo, "refs", "tags", mkdir=True)
    assert repo_dir(repo, "refs", "heads", mkdir=True)

    # .git/description
    with open(repo_file(repo, "description"), "w", encoding="utf-8") as f:
        f.write(
            "Unnamed repository; edit this file 'description' to name the repository.\n"
        )

    # .git/HEAD
    with open(repo_file(repo, "HEAD"), "w", encoding="utf-8") as f:
        f.write("ref: refs/heads/master\n")

    with open(repo_file(repo, "config"), "w", encoding="utf-8") as f:
        config = repo_default_config()
        config.write(f)

    return repo


def repo_find(path: str = ".", required: bool = True) -> GitRepository | None:
    path = os.path.realpath(path)
    if os.path.isdir(os.path.join(path, WYAG_DIR)):
        return GitRepository(path)

    # If we haven't returned, recurse in parent, if w
    parent = os.path.realpath(os.path.join(path, ".."))
    if parent == path:
        # Bottom case
        # os.path.join("/", "..") == "/":
        # If parent==path, then path is root.
        if required:
            raise RuntimeError("No git directory.")
        else:
            return None

    # Recursive case
    return repo_find(parent, required)


def repo_default_config() -> configparser.ConfigParser:
    ret = configparser.ConfigParser()

    ret.add_section("core")
    ret.set("core", "repositoryformatversion", "0")
    ret.set("core", "filemode", "false")
    ret.set("core", "bare", "false")

    return ret


class GitObject(object):

    def __init__(self, data=None):
        if data != None:
            self.deserialize(data)
        else:
            self.init()

    def serialize(self, repo: GitRepository) -> str:
        """This function MUST be implemented by subclasses.

        It must read the object's contents from self.data, a byte string,
        and do whatever it takes to convert it into a meaningful representation.
        What exactly that means depend on each subclass.
        """
        raise NotImplementedError("Unimplemented!")

    def deserialize(self, data):
        raise NotImplementedError("Unimplemented!")

    def init(self):
        pass  # Just do nothing. This is a reasonable default!


class GitBlob(GitObject):
    def __init__(self, data: str) -> typing.Self:
        self.format_str = b"blob"
        super().__init__(data)

    def serialize(self, repo: GitRepository) -> str:
        return self.blobdata

    def deserialize(self, data):
        self.blobdata = data


# kvlm -> Key-Value List with Message
def kvlm_parse(raw: str, start=0, dct=None) -> collections.OrderedDict:
    if not dct:
        dct = collections.OrderedDict()
        # You CANNOT declare the argument as dct=OrderedDict() or all
        # call to the functions will endlessly grow the same dict.

    # This function is recursive: it reads a key/value pair, then call
    # itself back with the new position.  So we first need to know
    # where we are: at a keyword, or already in the messageQ

    # We search for the next space and the next newline.
    spc = raw.find(b" ", start)
    nl = raw.find(b"\n", start)

    # If space appears before newline, we have a keyword.  Otherwise,
    # it's the final message, which we just read to the end of the file.

    # Base case
    # =========
    # If newline appears first (or there's no space at all, in which
    # case find returns -1), we assume a blank line.  A blank line
    # means the remainder of the data is the message.  We store it in
    # the dictionary, with None as the key, and return.
    if (spc < 0) or (nl < spc):
        assert nl == start
        dct[None] = raw[start + 1 :]
        return dct

    # Recursive case
    # ==============
    # we read a key-value pair and recurse for the next.
    key = raw[start:spc]

    # Find the end of the value.  Continuation lines begin with a
    # space, so we loop until we find a "\n" not followed by a space.
    end = start
    while True:
        end = raw.find(b"\n", end + 1)
        if raw[end + 1] != ord(" "):
            break

    # Grab the value
    # Also, drop the leading space on continuation lines
    value = raw[spc + 1 : end].replace(b"\n ", b"\n")

    # Don't overwrite existing data contents
    if key in dct:
        if type(dct[key]) == list:
            dct[key].append(value)
        else:
            dct[key] = [dct[key], value]
    else:
        dct[key] = value

    return kvlm_parse(raw, start=end + 1, dct=dct)


def kvlm_serialize(kvlm: collections.OrderedDict):
    ret = b""

    # Output fields
    for k in kvlm.keys():
        # Skip the message itself
        if k == None:
            continue
        val = kvlm[k]
        # Normalize to a list
        if type(val) != list:
            val = [val]

        for v in val:
            ret += k + b" " + (v.replace(b"\n", b"\n ")) + b"\n"

    # Append message
    ret += b"\n" + kvlm[None] + b"\n"

    return ret


class GitCommit(GitObject):
    def __init__(self, data: str) -> typing.Self:
        self.format_str = b"commit"
        self.kvlm = None
        super().__init__(data)

    def deserialize(self, data):
        self.kvlm = kvlm_parse(data)

    def serialize(self, repo: GitRepository):
        return kvlm_serialize(self.kvlm)

    def init(self):
        self.kvlm = dict()


class GitTreeLeaf(object):
    def __init__(self, mode: str, path: str, sha: str) -> typing.Self:
        self.mode = mode
        self.path = path
        self.sha = sha


def tree_parse_one(raw: str, start=0) -> tuple[int, GitTreeLeaf]:
    # Find the space terminator of the mode
    x = raw.find(b" ", start)
    assert x - start == 5 or x - start == 6

    # Read the mode
    mode: str = raw[start:x]
    if len(mode) == 5:
        # Normalize to six bytes.
        mode = b" " + mode

    # Find the NULL terminator of the path
    y = raw.find(b"\x00", x)
    # and read the path
    path: str = raw[x + 1 : y]

    # Read the SHA…
    raw_sha = int.from_bytes(raw[y + 1 : y + 21], "big")
    # and convert it into an hex string, padded to 40 chars
    # with zeros if needed.
    sha = format(raw_sha, "040x")
    return y + 21, GitTreeLeaf(mode, path.decode("utf8"), sha)


def tree_parse(raw) -> list[GitTreeLeaf]:
    pos = 0
    max_len = len(raw)
    ret = list()
    while pos < max_len:
        pos, data = tree_parse_one(raw, pos)
        ret.append(data)

    return ret


# Notice this isn't a comparison function, but a conversion function.
# Python's default sort doesn't accept a custom comparison function,
# like in most languages, but a `key` arguments that returns a new
# value, which is compared using the default rules.  So we just return
# the leaf name, with an extra / if it's a directory.
def tree_leaf_sort_key(leaf: GitTreeLeaf) -> str:
    if leaf.mode.startswith(b"10"):
        return leaf.path
    else:
        return leaf.path + "/"


class GitTree(GitObject):
    def __init__(self, data: str) -> typing.Self:
        self.format_str = b"tree"
        super().__init__(data)

    def deserialize(self, data):
        self.items = tree_parse(data)

    def serialize(self, repo: GitRepository):
        return tree_serialize(self)

    def init(self):
        self.items = list()


def tree_serialize(obj: GitTree) -> str:
    obj.items.sort(key=tree_leaf_sort_key)
    ret = b""
    for i in obj.items:
        ret += i.mode
        ret += b" "
        ret += i.path.encode("utf-8")
        ret += b"\x00"
        sha = int(i.sha, 16)
        ret += sha.to_bytes(20, byteorder="big")
    return ret


def ref_resolve(repo: GitRepository, ref: str) -> str:
    path = repo_file(repo, ref)

    # Sometimes, an indirect reference may be broken.  This is normal
    # in one specific case: we're looking for HEAD on a new repository
    # with no commits.  In that case, .git/HEAD points to "ref:
    # refs/heads/main", but .git/refs/heads/main doesn't exist yet
    # (since there's no commit for it to refer to).
    if not os.path.isfile(path):
        return None

    with open(path, "r", encoding="utf-8") as fp:
        data = fp.read()[:-1]
        # Drop final \n ^^^^^
    if data.startswith("ref: "):
        return ref_resolve(repo, data[5:])
    else:
        return data


def ref_list(repo: GitRepository, path: str = None) -> collections.OrderedDict:
    if not path:
        path = repo_dir(repo, "refs")
    ret = collections.OrderedDict()
    # Git shows refs sorted.  To do the same, we use
    # an OrderedDict and sort the output of listdir
    for f in sorted(os.listdir(path)):
        can = os.path.join(path, f)
        if os.path.isdir(can):
            ret[f] = ref_list(repo, can)
        else:
            ret[f] = ref_resolve(repo, can)

    return ret


class GitTag(GitCommit):
    format_str = b"tag"


class GitIndexEntry:
    def __init__(
        self,
        ctime=None,
        mtime=None,
        dev: int = None,
        ino: int = None,
        mode_type: int = None,
        mode_perms: int = None,
        uid: int = None,
        gid: int = None,
        fsize: int = None,
        sha: str = None,
        flag_assume_valid: bool = None,
        flag_stage: int = None,
        name: str = None,
    ):
        # The last time a file's metadata changed.  This is a pair
        # (timestamp in seconds, nanoseconds)
        self.ctime = ctime
        # The last time a file's data changed.  This is a pair
        # (timestamp in seconds, nanoseconds)
        self.mtime = mtime
        # The ID of device containing this file
        self.dev = dev
        # The file's inode number
        self.ino = ino
        # The object type, either b1000 (regular), b1010 (symlink),
        # b1110 (gitlink).
        self.mode_type = mode_type
        # The object permissions, an integer.
        self.mode_perms = mode_perms
        # User ID of owner
        self.uid = uid
        # Group ID of ownner
        self.gid = gid
        # Size of this object, in bytes
        self.fsize = fsize
        # The object's SHA
        self.sha = sha
        self.flag_assume_valid = flag_assume_valid
        self.flag_stage = flag_stage
        # Name of the object (full path this time!)
        self.name = name


class GitIndex:

    def __init__(self, version=2, entries=None):
        if not entries:
            entries = list()

        self.version = version
        self.entries: list[GitIndexEntry] = entries
        # self.ext = None
        # self.sha = None


def index_read(repo: GitRepository) -> GitIndex:
    index_file = repo_file(repo, "index")

    # New repositories have no index!
    if not os.path.exists(index_file):
        return GitIndex()

    with open(index_file, "rb") as f:
        raw = f.read()

    header = raw[:12]
    signature = header[:4]
    assert signature == b"DIRC"  # Stands for "DirCache"
    version = int.from_bytes(header[4:8], "big")
    assert version == 2, "wyag only supports index file version 2"
    count = int.from_bytes(header[8:12], "big")

    entries = list()

    content = raw[12:]
    idx = 0
    for i in range(0, count):
        # Read creation time, as a unix timestamp (seconds since
        # 1970-01-01 00:00:00, the "epoch")
        ctime_s = int.from_bytes(content[idx : idx + 4], "big")
        # Read creation time, as nanoseconds after that timestamps,
        # for extra precision.
        ctime_ns = int.from_bytes(content[idx + 4 : idx + 8], "big")
        # Same for modification time: first seconds from epoch.
        mtime_s = int.from_bytes(content[idx + 8 : idx + 12], "big")
        # Then extra nanoseconds
        mtime_ns = int.from_bytes(content[idx + 12 : idx + 16], "big")
        # Device ID
        dev = int.from_bytes(content[idx + 16 : idx + 20], "big")
        # Inode
        ino = int.from_bytes(content[idx + 20 : idx + 24], "big")
        # Ignored.
        unused = int.from_bytes(content[idx + 24 : idx + 26], "big")
        assert 0 == unused
        mode = int.from_bytes(content[idx + 26 : idx + 28], "big")
        mode_type = mode >> 12
        assert mode_type in [0b1000, 0b1010, 0b1110]
        mode_perms = mode & 0b0000000111111111
        # User ID
        uid = int.from_bytes(content[idx + 28 : idx + 32], "big")
        # Group ID
        gid = int.from_bytes(content[idx + 32 : idx + 36], "big")
        # Size
        fsize = int.from_bytes(content[idx + 36 : idx + 40], "big")
        # SHA (object ID).  We'll store it as a lowercase hex string
        # for consistency.
        sha = format(int.from_bytes(content[idx + 40 : idx + 60], "big"), "040x")
        # Flags we're going to ignore
        flags = int.from_bytes(content[idx + 60 : idx + 62], "big")
        # Parse flags
        flag_assume_valid = (flags & 0b1000000000000000) != 0
        flag_extended = (flags & 0b0100000000000000) != 0
        assert not flag_extended
        flag_stage = flags & 0b0011000000000000
        # Length of the name.  This is stored on 12 bits, some max
        # value is 0xFFF, 4095.  Since names can occasionally go
        # beyond that length, git treats 0xFFF as meaning at least
        # 0xFFF, and looks for the final 0x00 to find the end of the
        # name --- at a small, and probably very rare, performance
        # cost.
        name_length = flags & 0b0000111111111111

        # We've read 62 bytes so far.
        idx += 62

        if name_length < 0xFFF:
            assert content[idx + name_length] == 0x00
            raw_name = content[idx : idx + name_length]
            idx += name_length + 1
        else:
            print("Notice: Name is 0x{:X} bytes long.".format(name_length))
            # This probably wasn't tested enough.  It works with a
            # path of exactly 0xFFF bytes.  Any extra bytes broke
            # something between git, my shell and my filesystem.
            null_idx = content.find(b"\x00", idx + 0xFFF)
            raw_name = content[idx:null_idx]
            idx = null_idx + 1

        # Just parse the name as utf8.
        name = raw_name.decode("utf8")

        # Data is padded on multiples of eight bytes for pointer
        # alignment, so we skip as many bytes as we need for the next
        # read to start at the right position.

        idx = 8 * ceil(idx / 8)

        # And we add this entry to our list.
        entries.append(
            GitIndexEntry(
                ctime=(ctime_s, ctime_ns),
                mtime=(mtime_s, mtime_ns),
                dev=dev,
                ino=ino,
                mode_type=mode_type,
                mode_perms=mode_perms,
                uid=uid,
                gid=gid,
                fsize=fsize,
                sha=sha,
                flag_assume_valid=flag_assume_valid,
                flag_stage=flag_stage,
                name=name,
            )
        )

    return GitIndex(version=version, entries=entries)


def index_write(repo: GitRepository, index: GitIndex):
    with open(repo_file(repo, "index"), "wb") as f:

        # HEADER

        # Write the magic bytes.
        f.write(b"DIRC")
        # Write version number.
        f.write(index.version.to_bytes(4, "big"))
        # Write the number of entries.
        f.write(len(index.entries).to_bytes(4, "big"))

        # ENTRIES

        idx = 0
        for e in index.entries:
            f.write(e.ctime[0].to_bytes(4, "big"))
            f.write(e.ctime[1].to_bytes(4, "big"))
            f.write(e.mtime[0].to_bytes(4, "big"))
            f.write(e.mtime[1].to_bytes(4, "big"))
            f.write(e.dev.to_bytes(4, "big"))
            f.write(e.ino.to_bytes(4, "big"))

            # Mode
            mode = (e.mode_type << 12) | e.mode_perms
            f.write(mode.to_bytes(4, "big"))

            f.write(e.uid.to_bytes(4, "big"))
            f.write(e.gid.to_bytes(4, "big"))

            f.write(e.fsize.to_bytes(4, "big"))
            # @FIXME Convert back to int.
            f.write(int(e.sha, 16).to_bytes(20, "big"))

            flag_assume_valid = 0x1 << 15 if e.flag_assume_valid else 0

            name_bytes = e.name.encode("utf8")
            bytes_len = len(name_bytes)
            if bytes_len >= 0xFFF:
                name_length = 0xFFF
            else:
                name_length = bytes_len

            # We merge back three pieces of data (two flags and the
            # length of the name) on the same two bytes.
            f.write((flag_assume_valid | e.flag_stage | name_length).to_bytes(2, "big"))

            # Write back the name, and a final 0x00.
            f.write(name_bytes)
            f.write((0).to_bytes(1, "big"))

            idx += 62 + len(name_bytes) + 1

            # Add padding if necessary.
            if idx % 8 != 0:
                pad = 8 - (idx % 8)
                f.write((0).to_bytes(pad, "big"))
                idx += pad


def object_read(repo: GitRepository, sha: str) -> GitObject:
    """Read object sha from Git repository repo.  Return a
    GitObject whose exact type depends on the object."""

    path = repo_file(repo, "objects", sha[0:2], sha[2:])

    if not os.path.isfile(path):
        return None

    with open(path, "rb") as f:
        raw = zlib.decompress(f.read())

        # Read object type
        x = raw.find(b" ")
        fmt = raw[0:x]

        # Read and validate object size
        y = raw.find(b"\x00", x)
        size = int(raw[x:y].decode("ascii"))
        if size != len(raw) - y - 1:
            raise RuntimeError(f"Malformed object {sha}: bad length")

        # Pick constructor
        match fmt:
            case b"commit":
                git_object_type = GitCommit
            case b"tree":
                git_object_type = GitTree
            case b"tag":
                git_object_type = GitTag
            case b"blob":
                git_object_type = GitBlob
            case _:
                raise RuntimeError(
                    f"Unknown type {fmt.decode("ascii")} for object {sha}"
                )

        # Call constructor and return object
        return git_object_type(raw[y + 1 :])


def object_write(obj: GitObject, repo: GitRepository = None) -> str:
    # Serialize object data
    data = obj.serialize(repo)

    # Add header
    result = obj.format_str + b" " + str(len(data)).encode() + b"\x00" + data

    # Compute hash
    sha = hashlib.sha1(result).hexdigest()

    if repo:
        # Compute path
        path = repo_file(repo, "objects", sha[0:2], sha[2:], mkdir=True)

        if not os.path.exists(path):
            with open(path, "wb") as f:
                # Compress and write
                f.write(zlib.compress(result))
    return sha


def cmd_init(args: argparse.Namespace) -> GitRepository:
    repo_create(args.path)


def object_resolve(repo: GitRepository, name: str) -> list[str]:
    """Resolve name to an object hash in repo.

    This function is aware of:

     - the HEAD literal
        - short and long hashes
        - tags
        - branches
        - remote branches"""
    candidates = []
    hashRE = re.compile(r"^[0-9A-Fa-f]{4,40}$")

    # Empty string?  Abort.
    if not name.strip():
        return None

    # Head is nonambiguous
    if name == "HEAD":
        return [ref_resolve(repo, "HEAD")]

    # If it's a hex string, try for a hash.
    if hashRE.match(name):
        # This may be a hash, either small or full.  4 seems to be the
        # minimal length for git to consider something a short hash.
        # This limit is documented in man git-rev-parse
        name = name.lower()
        prefix = name[0:2]
        path = repo_dir(repo, "objects", prefix, mkdir=False)
        if path:
            rem = name[2:]
            for f in os.listdir(path):
                if f.startswith(rem):
                    # Notice a string startswith() itself, so this
                    # works for full hashes.
                    candidates.append(prefix + f)

    # Try for references.
    as_tag = ref_resolve(repo, "refs/tags/" + name)
    if as_tag:  # Did we find a tag?
        candidates.append(as_tag)

    as_branch = ref_resolve(repo, "refs/heads/" + name)
    if as_branch:  # Did we find a branch?
        candidates.append(as_branch)

    return candidates


def object_find(repo: GitRepository, name: str, fmt=None, follow=True) -> str:
    sha: list[str] = object_resolve(repo, name)

    if not sha:
        raise RuntimeError(f"No such reference {name}.")

    if len(sha) > 1:
        raise RuntimeError(
            "Ambiguous reference {0}: Candidates are:\n - {1}.".format(
                name, "\n - ".join(sha)
            )
        )

    sha: str = sha[0]

    if not fmt:
        return sha

    while True:
        obj = object_read(repo, sha)
        #     ^^^^^^^^^^^ < this is a bit agressive: we're reading
        # the full object just to get its type.  And we're doing
        # that in a loop, albeit normally short.  Don't expect
        # high performance here.

        if obj.format_str == fmt:
            return sha

        if not follow:
            return None

        # Follow tags
        if obj.format_str == b"tag":
            sha = obj.kvlm[b"object"].decode("ascii")
        elif obj.format_str == b"commit" and fmt == b"tree":
            sha = obj.kvlm[b"tree"].decode("ascii")
        else:
            return None


def cat_file(repo: GitRepository, obj: str, fmt=None):
    git_obj = object_read(repo, object_find(repo, obj, fmt=fmt))
    sys.stdout.buffer.write(git_obj.serialize(repo))


def cmd_cat_file(args: argparse.Namespace):
    repo = repo_find()
    cat_file(repo, args.object, fmt=args.type.encode())


def object_hash(fd: io.BufferedReader, format_str: str, repo=None) -> str:
    """Hash object, writing it to repo if provided."""
    data = fd.read()

    # Choose constructor according to fmt argument
    match format_str:
        case b"commit":
            obj = GitCommit(data)
        case b"tree":
            obj = GitTree(data)
        case b"tag":
            obj = GitTag(data)
        case b"blob":
            obj = GitBlob(data)
        case _:
            raise RuntimeError(f"Unknown type {format_str}!")

    return object_write(obj, repo)


def cmd_hash_object(args: argparse.Namespace):
    if args.write:
        repo = repo_find()
    else:
        repo = None

    with open(args.path, "rb") as fd:
        sha = object_hash(fd, args.type.encode(), repo)
        print(sha)


def log_graphviz(repo: str, sha: str, seen: set) -> None:
    if sha in seen:
        return

    seen.add(sha)

    commit: GitCommit = object_read(repo, sha)
    short_hash = sha[0:8]
    message: str = commit.kvlm[None].decode("utf8").strip()
    message = message.replace("\\", "\\\\")
    message = message.replace('"', '\\"')

    if "\n" in message:  # Keep only the first line
        message = message[: message.index("\n")]

    print('  c_{0} [label="{1}: {2}"]'.format(sha, sha[0:7], message))
    assert commit.format_str == b"commit"

    if not b"parent" in commit.kvlm.keys():
        # Base case: the initial commit.
        return

    parents: str = commit.kvlm[b"parent"]

    if type(parents) != list:
        parents = [parents]

    for p in parents:
        p = p.decode("ascii")
        print(f"  c_{sha} -> c_{p};")
        log_graphviz(repo, p, seen)


def cmd_log(args):
    repo = repo_find()

    print("digraph wyaglog{")
    print("  node[shape=rect]")
    log_graphviz(repo, object_find(repo, args.commit), set())
    print("}")


def ls_tree(repo: GitRepository, ref: str, recursive=None, prefix=""):
    sha = object_find(repo, ref, fmt=b"tree")
    obj: GitTree = object_read(repo, sha)
    for item in obj.items:
        if len(item.mode) == 5:
            item_type = item.mode[0:1]
        else:
            item_type = item.mode[0:2]

        match item_type:  # Determine the type.
            case b"04":
                item_type = "tree"
            case b"10":
                item_type = "blob"  # A regular file.
            case b"12":
                item_type = "blob"  # A symlink. Blob contents is link target.
            case b"16":
                item_type = "commit"  # A submodule
            case _:
                raise RuntimeError(f"Weird tree leaf mode {item.node}")

        if not (recursive and item_type == "tree"):  # This is a leaf
            print(
                "{0} {1} {2}\t{3}".format(
                    "0" * (6 - len(item.mode)) + item.mode.decode("ascii"),
                    # Git's ls-tree displays the type
                    # of the object pointed to.  We can do that too :)
                    item_type,
                    item.sha,
                    os.path.join(prefix, item.path),
                )
            )
        else:  # This is a branch, recurse
            ls_tree(repo, item.sha, recursive, os.path.join(prefix, item.path))


def cmd_ls_tree(args: argparse.Namespace):
    repo = repo_find()
    ls_tree(repo, args.tree, args.recursive)


def tree_checkout(repo: GitRepository, tree: GitObject, path: str) -> None:
    for item in tree.items:
        obj = object_read(repo, item.sha)
        dest = os.path.join(path, item.path)

        if obj.format_str == b"tree":
            os.mkdir(dest)
            tree_checkout(repo, obj, dest)
        elif obj.format_str == b"blob":
            # @TODO Support symlinks (identified by mode 12****)
            with open(dest, "wb") as f:
                f.write(obj.blobdata)


def cmd_checkout(args: argparse.Namespace):
    repo = repo_find()

    obj: GitObject = object_read(repo, object_find(repo, args.commit))

    # If the object is a commit, we grab its tree
    if obj.format_str == b"commit":
        obj = object_read(repo, obj.kvlm[b"tree"].decode("ascii"))

    # Verify that path is an empty directory
    if os.path.exists(args.path):
        if not os.path.isdir(args.path):
            raise NotADirectoryError(f"Not a directory {args.path}!")
        if os.listdir(args.path):
            raise RuntimeError(f"Not empty {args.path}!")
    else:
        os.makedirs(args.path)

    tree_checkout(repo, obj, os.path.realpath(args.path))


def show_ref(
    repo: GitRepository,
    refs: collections.OrderedDict,
    with_hash: bool = True,
    prefix: str = "",
):
    for k, v in refs.items():
        if isinstance(v, str):
            print(
                "{0}{1}{2}".format(
                    v + " " if with_hash else "", prefix + "/" if prefix else "", k
                )
            )
        else:
            show_ref(
                repo,
                v,
                with_hash=with_hash,
                prefix="{0}{1}{2}".format(prefix, "/" if prefix else "", k),
            )


def cmd_show_ref(args: argparse.Namespace):
    repo = repo_find()
    refs = ref_list(repo)
    show_ref(repo, refs, prefix="refs")


def ref_create(repo: GitRepository, ref_name: str, sha: str):
    with open(repo_file(repo, "refs/" + ref_name), "w", encoding="utf-8") as fp:
        fp.write(sha + "\n")


def tag_create(
    repo: GitRepository, name: str, ref: str, create_tag_object=False, type=None
):
    # get the GitObject from the object reference
    sha = object_find(repo, ref)

    if create_tag_object:
        # create tag object (commit)
        tag = GitTag(repo)
        tag.kvlm = collections.OrderedDict()
        tag.kvlm[b"object"] = sha.encode()
        tag.kvlm[b"type"] = b"commit"
        tag.kvlm[b"tag"] = name.encode()
        # Feel free to let the user give their name!
        # Notice you can fix this after commit, read on!
        tag.kvlm[b"tagger"] = b"Wyag <wyag@example.com>"
        # …and a tag message!
        tag.kvlm[None] = (
            b"A tag generated by wyag, which won't let you customize the message!"
        )
        tag_sha = object_write(tag)
        # create reference
        ref_create(repo, "tags/" + name, tag_sha)
    else:
        # create lightweight tag (ref)
        ref_create(repo, "tags/" + name, sha)


def cmd_tag(args: argparse.Namespace):
    repo = repo_find()

    if args.name:
        tag_create(
            repo,
            args.name,
            args.object,
            type="object" if args.create_tag_object else "ref",
        )
    else:
        refs = ref_list(repo)
        show_ref(repo, refs["tags"], with_hash=False)


def cmd_rev_parse(args):
    if args.type:
        fmt = args.type.encode()
    else:
        fmt = None

    repo = repo_find()

    print(object_find(repo, args.name, fmt, follow=True))


def cmd_ls_files(args):
    repo = repo_find()
    index = index_read(repo)
    if args.verbose:
        print(
            "Index file format v{}, containing {} entries.".format(
                index.version, len(index.entries)
            )
        )

    for e in index.entries:
        print(e.name)
        if args.verbose:
            print(
                "  {} with perms: {:o}".format(
                    {0b1000: "regular file", 0b1010: "symlink", 0b1110: "git link"}[
                        e.mode_type
                    ],
                    e.mode_perms,
                )
            )
            print("  on blob: {}".format(e.sha))
            print(
                "  created: {}.{}, modified: {}.{}".format(
                    datetime.fromtimestamp(e.ctime[0]),
                    e.ctime[1],
                    datetime.fromtimestamp(e.mtime[0]),
                    e.mtime[1],
                )
            )
            print("  device: {}, inode: {}".format(e.dev, e.ino))
            print(
                "  user: {} ({})  group: {} ({})".format(
                    pwd.getpwuid(e.uid).pw_name,
                    e.uid,
                    grp.getgrgid(e.gid).gr_name,
                    e.gid,
                )
            )
            print(
                "  flags: stage={} assume_valid={}".format(
                    e.flag_stage, e.flag_assume_valid
                )
            )


def gitignore_parse1(raw: str) -> tuple[str, bool]:
    raw = raw.strip()  # Remove leading/trailing spaces

    if not raw or raw[0] == "#":
        return None
    if raw[0] == "!":
        return (raw[1:], False)
    if raw[0] == "\\":
        return (raw[1:], True)

    return (raw, True)


def gitignore_parse(lines: list[str]) -> list[tuple[str, bool]]:
    ret = list()

    for line in lines:
        parsed = gitignore_parse1(line)
        if parsed:
            ret.append(parsed)

    return ret


class GitIgnore:
    def __init__(self, absolute, scoped):
        self.absolute: list = absolute
        self.scoped: dict = scoped


def gitignore_read(repo: GitRepository) -> GitIgnore:
    ret = GitIgnore(absolute=list(), scoped=dict())

    # Read local configuration in .git/info/exclude
    local_repo_file = os.path.join(repo.git_dir, "info/exclude")
    if os.path.exists(local_repo_file):
        with open(local_repo_file, "r", encoding="utf-8") as f:
            ret.absolute.append(gitignore_parse(f.readlines()))

    # Global configuration
    if "XDG_CONFIG_HOME" in os.environ:
        config_home = os.environ["XDG_CONFIG_HOME"]
    else:
        config_home = os.path.expanduser("~/.config")
    global_file = os.path.join(config_home, "git/ignore")

    if os.path.exists(global_file):
        with open(global_file, "r", encoding="utf-8") as f:
            ret.absolute.append(gitignore_parse(f.readlines()))

    # .gitignore files in the index
    index = index_read(repo)

    for entry in index.entries:
        if entry.name == ".gitignore" or entry.name.endswith("/.gitignore"):
            dir_name = os.path.dirname(entry.name)
            contents: GitObject = object_read(repo, entry.sha)
            lines = contents.blobdata.decode("utf8").splitlines()
            ret.scoped[dir_name] = gitignore_parse(lines)
    return ret


def check_ignore1(rules, path) -> bool | None:
    result = None
    for pattern, value in rules:
        if fnmatch(path, pattern):
            result = value
    return result


def check_ignore_scoped(rules: dict, path: str) -> bool | None:
    parent = os.path.dirname(path)
    while True:
        if parent in rules:
            result = check_ignore1(rules[parent], path)
            if result != None:
                return result
        if parent == "":
            break
        parent = os.path.dirname(parent)
    return None


def check_ignore_absolute(rules: list, path: str) -> bool | None:
    parent = os.path.dirname(path)
    for ruleset in rules:
        result = check_ignore1(ruleset, path)
        if result != None:
            return result
    return False  # This is a reasonable default at this point.


def check_ignore(rules: GitIgnore, path: str) -> bool | None:
    if os.path.isabs(path):
        raise RuntimeError(
            "This function requires path to be relative to the repository's root"
        )

    result = check_ignore_scoped(rules.scoped, path)
    if result != None:
        return result

    return check_ignore_absolute(rules.absolute, path)


def cmd_check_ignore(args: argparse.Namespace):
    repo = repo_find()
    rules = gitignore_read(repo)
    for path in args.path:
        if check_ignore(rules, path):
            print(path)


def branch_get_active(repo: GitRepository) -> str | bool:
    with open(repo_file(repo, "HEAD"), "r", encoding="utf-8") as f:
        head = f.read()

    if head.startswith("ref: refs/heads/"):
        return head[16:-1]

    return False


def cmd_status_branch(repo):
    branch = branch_get_active(repo)
    if branch:
        print(f"On branch {branch}.")
    else:
        print("HEAD detached at {}".format(object_find(repo, "HEAD")))


def tree_to_dict(repo: GitRepository, ref: str, prefix: str = ""):
    ret = dict()
    tree_sha = object_find(repo, ref, fmt=b"tree")
    tree = object_read(repo, tree_sha)

    for leaf in tree.items:
        full_path = os.path.join(prefix, leaf.path)

        # We read the object to extract its type (this is uselessly
        # expensive: we could just open it as a file and read the
        # first few bytes)
        is_subtree = leaf.mode.startswith(b"04")

        # Depending on the type, we either store the path (if it's a
        # blob, so a regular file), or recurse (if it's another tree,
        # so a subdir)
        if is_subtree:
            ret.update(tree_to_dict(repo, leaf.sha, full_path))
        else:
            ret[full_path] = leaf.sha

    return ret


def cmd_status_head_index(repo: GitRepository, index: GitIndex):
    print("Changes to be committed:")

    head = tree_to_dict(repo, "HEAD")
    for entry in index.entries:
        if entry.name in head:
            if head[entry.name] != entry.sha:
                print("  modified:", entry.name)
            del head[entry.name]  # Delete the key
        else:
            print("  added:   ", entry.name)

    # Keys still in HEAD are files that we haven't met in the index,
    # and thus have been deleted.
    for entry in head.keys():
        print("  deleted: ", entry)


def cmd_status_index_worktree(repo, index):
    print("Changes not staged for commit:")

    ignore = gitignore_read(repo)

    gitdir_prefix = repo.gitdir + os.path.sep

    all_files = list()

    # We begin by walking the filesystem
    for root, _, files in os.walk(repo.worktree, True):
        if root == repo.gitdir or root.startswith(gitdir_prefix):
            continue
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, repo.worktree)
            all_files.append(rel_path)

    # We now traverse the index, and compare real files with the cached
    # versions.

    for entry in index.entries:
        full_path = os.path.join(repo.worktree, entry.name)

        # That file *name* is in the index

        if not os.path.exists(full_path):
            print("  deleted: ", entry.name)
        else:
            stat = os.stat(full_path)

            # Compare metadata
            ctime_ns = entry.ctime[0] * 10**9 + entry.ctime[1]
            mtime_ns = entry.mtime[0] * 10**9 + entry.mtime[1]
            if (stat.st_ctime_ns != ctime_ns) or (stat.st_mtime_ns != mtime_ns):
                # If different, deep compare.
                # @FIXME This *will* crash on symlinks to dir.
                with open(full_path, "rb") as fd:
                    new_sha = object_hash(fd, b"blob", None)
                    # If the hashes are the same, the files are actually the same.
                    same = entry.sha == new_sha

                    if not same:
                        print("  modified:", entry.name)

        if entry.name in all_files:
            all_files.remove(entry.name)

    print()
    print("Untracked files:")

    for f in all_files:
        # @TODO If a full directory is untracked, we should display
        # its name without its contents.
        if not check_ignore(ignore, f):
            print(" ", f)


def cmd_status(_):
    repo = repo_find()
    index = index_read(repo)

    cmd_status_branch(repo)
    cmd_status_head_index(repo, index)
    print()
    cmd_status_index_worktree(repo, index)


def rm(
    repo: GitRepository,
    paths: list[str],
    delete: bool = True,
    skip_missing: bool = False,
):
    # Find and read the index
    index = index_read(repo)

    work_tree = repo.work_tree + os.sep

    # Make paths absolute
    abs_paths = []
    for path in paths:
        abspath = os.path.abspath(path)
        if abspath.startswith(work_tree):
            abs_paths.append(abspath)
        else:
            raise RuntimeError("Cannot remove paths outside of worktree: {paths}")

    kept_entries = []
    remove = []

    for e in index.entries:
        full_path = os.path.join(repo.work_tree, e.name)

        if full_path in abs_paths:
            remove.append(full_path)
            abs_paths.remove(full_path)
        else:
            kept_entries.append(e)  # Preserve entry

    if len(abs_paths) > 0 and not skip_missing:
        raise RuntimeError("Cannot remove paths not in the index: {abs_paths}")

    if delete:
        for path in remove:
            os.unlink(path)

    index.entries = kept_entries
    index_write(repo, index)


def cmd_rm(args):
    repo = repo_find()
    rm(repo, args.path)


def add(
    repo: GitRepository,
    paths: list[str],
    delete: bool = True,
    skip_missing: bool = False,
) -> None:
    # First remove all paths from the index, if they exist.
    rm(repo, paths, delete=False, skip_missing=True)

    work_tree = repo.work_tree + os.sep

    # Convert the paths to pairs: (absolute, relative_to_worktree).
    # Also delete them from the index if they're present.
    clean_paths = list()
    for path in paths:
        abspath = os.path.abspath(path)
        if not (abspath.startswith(work_tree) and os.path.isfile(abspath)):
            raise RuntimeError(f"Not a file, or outside the worktree: {paths}")
        relpath = os.path.relpath(abspath, repo.work_tree)
        clean_paths.append((abspath, relpath))

        # Find and read the index.  It was modified by rm.  (This isn't
        # optimal, good enough for wyag!)
        #
        # @FIXME, though: we could just
        # move the index through commands instead of reading and writing
        # it over again.
        index = index_read(repo)

        for abspath, relpath in clean_paths:
            with open(abspath, "rb") as fd:
                sha = object_hash(fd, b"blob", repo)

        stat = os.stat(abspath)

        ctime_s = int(stat.st_ctime)
        ctime_ns = stat.st_ctime_ns % 10**9
        mtime_s = int(stat.st_mtime)
        mtime_ns = stat.st_mtime_ns % 10**9

        entry = GitIndexEntry(
            ctime=(ctime_s, ctime_ns),
            mtime=(mtime_s, mtime_ns),
            dev=stat.st_dev,
            ino=stat.st_ino,
            mode_type=0b1000,
            mode_perms=0o644,
            uid=stat.st_uid,
            gid=stat.st_gid,
            fsize=stat.st_size,
            sha=sha,
            flag_assume_valid=False,
            flag_stage=False,
            name=relpath,
        )
        index.entries.append(entry)

        # Write the index back
        index_write(repo, index)


def cmd_add(args: argparse.Namespace):
    repo = repo_find()
    add(repo, args.path)


def gitconfig_read():
    xdg_config_home = (
        os.environ["XDG_CONFIG_HOME"]
        if "XDG_CONFIG_HOME" in os.environ
        else "~/.config"
    )
    configfiles = [
        os.path.expanduser(os.path.join(xdg_config_home, "git/config")),
        os.path.expanduser("~/.gitconfig"),
    ]

    config = configparser.ConfigParser()
    config.read(configfiles)
    return config


def gitconfig_user_get(config):
    if "user" in config:
        if "name" in config["user"] and "email" in config["user"]:
            return "{} <{}>".format(config["user"]["name"], config["user"]["email"])
    return None


def tree_from_index(repo: GitRepository, index: GitIndex):
    contents = dict()
    contents[""] = list()

    # Enumerate entries, and turn them into a dictionary where keys
    # are directories, and values are lists of directory contents.
    for entry in index.entries:
        dirname = os.path.dirname(entry.name)

        # We create all dictonary entries up to root ("").  We need
        # them *all*, because even if a directory holds no files it
        # will contain at least a tree.
        key = dirname
        while key != "":
            if not key in contents:
                contents[key] = list()
            key = os.path.dirname(key)

        # For now, simply store the entry in the list.
        contents[dirname].append(entry)

    # Get keys (= directories) and sort them by length, descending.
    # This means that we'll always encounter a given path before its
    # parent, which is all we need, since for each directory D we'll
    # need to modify its parent P to add D's tree.
    sorted_paths = sorted(contents.keys(), key=len, reverse=True)

    # This variable will store the current tree's SHA-1.  After we're
    # done iterating over our dict, it will contain the hash for the
    # root tree.
    sha = None

    # We ge through the sorted list of paths (dict keys)
    for path in sorted_paths:
        # Prepare a new, empty tree object
        tree = GitTree()

        # Add each entry to our new tree, in turn
        for entry in contents[path]:
            # An entry can be a normal GitIndexEntry read from the
            # index, or a tree we've created.
            if isinstance(entry, GitIndexEntry):  # Regular entry (a file)

                # We transcode the mode: the entry stores it as integers,
                # we need an octal ASCII representation for the tree.
                leaf_mode = "{:02o}{:04o}".format(
                    entry.mode_type, entry.mode_perms
                ).encode("ascii")
                leaf = GitTreeLeaf(
                    mode=leaf_mode, path=os.path.basename(entry.name), sha=entry.sha
                )
            else:  # Tree.  We've stored it as a pair: (basename, SHA)
                leaf = GitTreeLeaf(mode=b"040000", path=entry[0], sha=entry[1])

            tree.items.append(leaf)

        # Write the new tree object to the store.
        sha = object_write(tree, repo)

        # Add the new tree hash to the current dictionary's parent, as
        # a pair (basename, SHA)
        parent = os.path.dirname(path)
        base = os.path.basename(
            path
        )  # The name without the path, eg main.go for src/main.go
        contents[parent].append((base, sha))

    return sha


def commit_create(repo: GitRepository, tree: str, parent, author, timestamp, message):
    commit = GitCommit(tree)  # Create the new commit object.
    commit.kvlm[b"tree"] = tree.encode("ascii")
    if parent:
        commit.kvlm[b"parent"] = parent.encode("ascii")

    # Format timezone
    offset = int(timestamp.astimezone().utcoffset().total_seconds())
    hours = offset // 3600
    minutes = (offset % 3600) // 60
    tz = "{}{:02}{:02}".format("+" if offset > 0 else "-", hours, minutes)

    author = author + timestamp.strftime(" %s ") + tz

    commit.kvlm[b"author"] = author.encode("utf8")
    commit.kvlm[b"committer"] = author.encode("utf8")
    commit.kvlm[None] = message.encode("utf8")

    return object_write(commit, repo)


def cmd_commit(args):
    repo = repo_find()
    index = index_read(repo)
    # Create trees, grab back SHA for the root tree.
    tree = tree_from_index(repo, index)

    # Create the commit object itself
    commit = commit_create(
        repo,
        tree,
        object_find(repo, "HEAD"),
        gitconfig_user_get(gitconfig_read()),
        datetime.now(),
        args.message,
    )

    # Update HEAD so our commit is now the tip of the active branch.
    active_branch = branch_get_active(repo)
    if active_branch:  # If we're on a branch, we update refs/heads/BRANCH
        with open(
            repo_file(repo, os.path.join("refs/heads", active_branch)),
            "w",
            encoding="utf-8",
        ) as fd:
            fd.write(commit + "\n")
    else:  # Otherwise, we update HEAD itself.
        with open(repo_file(repo, "HEAD"), "w", encoding="utf-8") as fd:
            fd.write("\n")

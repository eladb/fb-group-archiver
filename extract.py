"""Heuristic normalizer for Facebook GraphQL payloads.

Facebook's GraphQL response shapes are undocumented and get reshuffled without
notice, so nothing here assumes a fixed path. Everything works by walking the
payload and recognizing nodes structurally. When a shape changes, the raw
capture is still intact -- re-run `scrape.py reparse` rather than re-crawling.
"""

import hashlib
import os
import re
import secrets
from pathlib import Path

MAX_DEPTH = 40

# ---- pseudonymization ------------------------------------------------
# On by default: the archive keeps every post and comment body, timestamp and
# count, but replaces author identities with a stable salted pseudonym so the
# corpus is not a register of named people. Raw payloads are deliberately NOT
# rewritten, so running `reparse` with FBGROUP_PSEUDONYMIZE=0 recovers real
# identities if they are ever legitimately needed.
PSEUDONYMIZE = os.environ.get("FBGROUP_PSEUDONYMIZE", "1") != "0"
SALT_FILE = Path(os.environ.get("FBGROUP_SALT_FILE", ".pseudonym-salt"))

_salt_cache = None


def _salt() -> bytes:
    """Per-archive random salt, created once and reused so pseudonyms are stable."""
    global _salt_cache
    if _salt_cache is None:
        if SALT_FILE.exists():
            _salt_cache = SALT_FILE.read_bytes().strip()
        else:
            _salt_cache = secrets.token_hex(32).encode()
            SALT_FILE.write_bytes(_salt_cache)
            try:
                SALT_FILE.chmod(0o600)
            except OSError:
                pass
    return _salt_cache


def pseudonymize(author_id, author_name):
    """Map an author to a stable opaque handle, or (None, None) if unidentified."""
    if not PSEUDONYMIZE:
        return author_id, author_name
    if not author_id and not author_name:
        return None, None
    seed = str(author_id or author_name).encode()
    digest = hashlib.blake2b(seed, key=_salt()[:64], digest_size=8).hexdigest()
    return f"anon:{digest}", None

MEDIA_URL_KEYS = {
    "uri", "src", "playable_url", "playable_url_quality_hd", "playable_url_dash_manifest",
    "browser_native_hd_url", "browser_native_sd_url", "image_url",
}
CDN_RE = re.compile(r"https://(scontent|video)[-\w.]*\.(fbcdn|xx\.fbcdn)\.net/", re.I)


def walk(obj, depth: int = 0):
    """Yield every dict nested anywhere inside obj."""
    if depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, depth + 1)


def dig(obj, *path, default=None):
    """Traverse dict keys / list indices, returning default on any miss."""
    cur = obj
    for step in path:
        if isinstance(step, int):
            if not isinstance(cur, list) or len(cur) <= step:
                return default
            cur = cur[step]
        else:
            if not isinstance(cur, dict) or step not in cur:
                return default
            cur = cur[step]
    return cur if cur is not None else default


def find_first(node, predicate, depth: int = 0):
    for d in walk(node, depth):
        if predicate(d):
            return d
    return None


# ---- node recognition ------------------------------------------------

def is_post_node(d) -> bool:
    if not isinstance(d, dict):
        return False
    tn = d.get("__typename")
    if tn == "Story" and ("post_id" in d or "creation_time" in d):
        return True
    # Some feed edges carry post_id without a __typename on the same level.
    if "post_id" in d and ("message" in d or "comet_sections" in d or "feedback" in d):
        return True
    return False


def is_comment_node(d) -> bool:
    return (
        isinstance(d, dict)
        and d.get("__typename") == "Comment"
        and "id" in d
        and ("body" in d or "created_time" in d)
    )


# Keys whose subtrees describe the *viewer* -- the account running the scrape --
# rather than the author of the content. Attributing a post or comment to the
# scraping account is worse than leaving it null, so these are never searched.
VIEWER_KEYS = {
    "viewer_actor", "viewer", "viewer_feedback_reaction_info", "actor_provider",
    "comment_composer_placeholder", "comet_composer", "if_viewer_can_comment_anonymously",
    "owning_profile", "comment_composer",
}

# GroupAnonAuthorProfile is how Facebook represents a member who posted
# anonymously: a stable per-group handle like "CuriousOtter1234" instead of a
# real profile. It is a legitimate author, and treating it as one keeps those
# contributions attributable to each other without identifying anyone.
ACTOR_TYPENAMES = ("User", "Page", "Group", "GroupAnonAuthorProfile")


def is_actor_node(d) -> bool:
    return (
        isinstance(d, dict)
        and d.get("__typename") in ACTOR_TYPENAMES
        and "name" in d
        and "id" in d
    )


# ---- field extraction ------------------------------------------------

def _text(node):
    """Post body text. Prefer the story message; fall back to any message.text."""
    for path in (
        ("message", "text"),
        ("comet_sections", "content", "story", "message", "text"),
        ("content", "story", "message", "text"),
    ):
        val = dig(node, *path)
        if isinstance(val, str) and val:
            return val
    msg = find_first(node, lambda d: isinstance(d.get("text"), str) and "ranges" in d)
    return msg.get("text") if msg else None


def _walk_excluding_viewer(obj, depth: int = 0):
    """Like walk(), but never descends into viewer-context subtrees."""
    if depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        yield obj
        for key, val in obj.items():
            if key in VIEWER_KEYS:
                continue
            yield from _walk_excluding_viewer(val, depth + 1)
    elif isinstance(obj, list):
        for val in obj:
            yield from _walk_excluding_viewer(val, depth + 1)


def _actor(node):
    """Resolve the author of a post or comment.

    Explicit author fields are tried first. Only then is the subtree searched,
    and that search skips viewer context: a payload carries the scraping
    account's own profile in several places, and an unguarded search reaches it
    whenever the real author is absent or is an unrecognized type -- silently
    attributing other people's words to the person running the archive.
    """
    for path in (("actors", 0), ("author",), ("comet_sections", "actor_photo", "story", "actors", 0)):
        cand = dig(node, *path)
        if is_actor_node(cand):
            return cand.get("id"), cand.get("name")
    for cand in _walk_excluding_viewer(node):
        if is_actor_node(cand):
            return cand.get("id"), cand.get("name")
    return None, None


def _created_at(node):
    val = node.get("creation_time") or node.get("created_time")
    if isinstance(val, (int, float)):
        return int(val)
    hit = find_first(
        node,
        lambda d: isinstance(d.get("creation_time") or d.get("created_time"), (int, float)),
    )
    if hit:
        return int(hit.get("creation_time") or hit.get("created_time"))
    return None


def _feedback(node):
    """Reaction / comment / share counts, wherever the feedback object landed.

    Two shapes have to be tolerated. The story's own ``feedback`` key is
    frequently a stub -- {id, associated_group, owning_profile} -- while the
    real counts sit deeper in the UFI subtree, so finding a dict at
    ``node["feedback"]`` must not stop the search. And counts arrive either
    wrapped as {"count": N} or as a bare number, depending on the renderer.
    """
    def as_int(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return int(v)

    def count_of(v):
        return as_int(v.get("count")) if isinstance(v, dict) else as_int(v)

    reactions = comments = shares = None
    for d in walk(node):
        if reactions is None:
            reactions = count_of(d.get("reaction_count"))
        if reactions is None:
            reactions = count_of(d.get("reactors"))
        if comments is None:
            comments = as_int(d.get("total_comment_count"))
        if comments is None:
            sub = d.get("comments")
            if isinstance(sub, dict):
                comments = as_int(sub.get("total_count"))
        if comments is None:
            comments = as_int(dig(d, "comment_rendering_instance", "comments", "total_count"))
        if shares is None:
            shares = count_of(d.get("share_count"))
        if reactions is not None and comments is not None and shares is not None:
            break
    return reactions, comments, shares


def _url(node, group_id, post_id):
    for key in ("wwwURL", "permalink_url", "url"):
        val = node.get(key)
        if isinstance(val, str) and "facebook.com" in val:
            return val.split("?")[0]
    if group_id and post_id:
        return f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/"
    return None


def media_urls(node):
    """Every CDN asset URL reachable from this node, with a coarse kind label."""
    found = {}
    for d in walk(node):
        for key, val in d.items():
            if key in MEDIA_URL_KEYS and isinstance(val, str) and CDN_RE.search(val):
                kind = "video" if ("video" in val or "playable" in key or "native" in key) else "image"
                # Keep the largest image variant we see for a given base asset.
                found.setdefault(val, kind)
    return found


# ---- public API ------------------------------------------------------

def normalize_post(node, group_id=None, raw_ref=None):
    post_id = node.get("post_id") or node.get("id")
    if not isinstance(post_id, str) or not post_id:
        return None
    author_id, author_name = pseudonymize(*_actor(node))
    reactions, comments, shares = _feedback(node)
    media = media_urls(node)
    return {
        "id": post_id,
        "group_id": group_id,
        "author_id": author_id,
        "author_name": author_name,
        "created_at": _created_at(node),
        "url": _url(node, group_id, post_id),
        "text": _text(node),
        "reaction_count": reactions,
        "comment_count": comments,
        "share_count": shares,
        "attachments": [{"url": u, "kind": k} for u, k in media.items()],
        "raw_ref": raw_ref,
    }


def normalize_comment(node, post_id=None, raw_ref=None):
    cid = node.get("id")
    if not isinstance(cid, str) or not cid:
        return None
    author_id, author_name = pseudonymize(*_actor(node))
    body = dig(node, "body", "text")
    if not isinstance(body, str):
        body = None
    # Replies name their parent under comment_direct_parent; the older
    # parent_comment / parent_feedback spellings are kept as fallbacks.
    parent = (
        dig(node, "comment_direct_parent", "id")
        or dig(node, "parent_comment", "id")
        or dig(node, "parent_feedback", "id")
    )
    media = media_urls(node)
    return {
        "id": cid,
        "post_id": post_id or dig(node, "feedback", "associated_story", "post_id"),
        "parent_id": parent,
        "author_id": author_id,
        "author_name": author_name,
        "created_at": _created_at(node),
        "text": body,
        "attachments": [{"url": u, "kind": k} for u, k in media.items()],
        "raw_ref": raw_ref,
    }


def harvest(payload, group_id=None, raw_ref=None):
    """Pull every post and comment out of one GraphQL payload."""
    posts, comments = [], []
    seen_posts, seen_comments = set(), set()
    for node in walk(payload):
        if is_post_node(node):
            p = normalize_post(node, group_id, raw_ref)
            if p and p["id"] not in seen_posts:
                seen_posts.add(p["id"])
                posts.append(p)
        elif is_comment_node(node):
            c = normalize_comment(node, None, raw_ref)
            if c and c["id"] not in seen_comments:
                seen_comments.add(c["id"])
                comments.append(c)
    return posts, comments


def typename_census(payload):
    """What __typenames appear in a payload -- used by `inspect` to tighten parsing."""
    census = {}
    for d in walk(payload):
        tn = d.get("__typename")
        if isinstance(tn, str):
            census[tn] = census.get(tn, 0) + 1
    return census

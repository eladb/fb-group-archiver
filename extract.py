"""Heuristic normalizer for Facebook GraphQL payloads.

Facebook's GraphQL response shapes are undocumented and get reshuffled without
notice, so nothing here assumes a fixed path. Everything works by walking the
payload and recognizing nodes structurally. When a shape changes, the raw
capture is still intact -- re-run `scrape.py reparse` rather than re-crawling.
"""

import re

MAX_DEPTH = 40

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


def is_actor_node(d) -> bool:
    return (
        isinstance(d, dict)
        and d.get("__typename") in ("User", "Page", "Group")
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


def _actor(node):
    actor = dig(node, "actors", 0)
    if not is_actor_node(actor):
        actor = find_first(node, is_actor_node)
    if not actor:
        return None, None
    return actor.get("id"), actor.get("name")


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
    """Reaction / comment / share counts, wherever the feedback object landed."""
    fb = node.get("feedback")
    if not isinstance(fb, dict):
        fb = find_first(
            node,
            lambda d: "reaction_count" in d or "total_comment_count" in d or "share_count" in d,
        )
    if not isinstance(fb, dict):
        return None, None, None
    reactions = dig(fb, "reaction_count", "count")
    if reactions is None:
        reactions = dig(fb, "reactors", "count")
    comments = dig(fb, "total_comment_count")
    if comments is None:
        comments = dig(fb, "comment_rendering_instance", "comments", "total_count")
    if comments is None:
        comments = dig(fb, "comments", "total_count")
    shares = dig(fb, "share_count", "count")
    as_int = lambda v: int(v) if isinstance(v, (int, float)) else None
    return as_int(reactions), as_int(comments), as_int(shares)


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
    author_id, author_name = _actor(node)
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
    author_id, author_name = _actor(node)
    body = dig(node, "body", "text")
    if not isinstance(body, str):
        body = None
    parent = dig(node, "parent_comment", "id") or dig(node, "parent_feedback", "id")
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

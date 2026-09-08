import base64
import html
import json
import os
import re
import threading
import time
from urllib.parse import urljoin, quote

import requests
from flask import Flask, Response, request

app = Flask(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")

STREAM_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://hamis.romponalis.st/",
    "Origin": "https://hamis.romponalis.st",
}

M3U8_TTL = 480        # upstream URL'deki token zaman asimli -> kisa cache
CHANLIST_TTL = 6 * 3600

CACHE = {}
CACHE_LOCK = threading.Lock()


def _get(url, referer):
    h = {"User-Agent": UA, "Referer": referer}
    return requests.get(url, headers=h, timeout=15)


def extract_m3u8(cid):
    """dlive.sx -> stream sayfasi -> daddy iframe -> base64 m3u8. requests only."""
    s = _get(f"https://dlive.sx/stream/stream-{cid}.php",
             f"https://dlive.sx/watch.php?id={cid}").text
    m = re.search(r'src="(https://hamis\.romponalis\.st[^"]+)"', s)
    if not m:
        raise ValueError("daddy iframe bulunamadi")
    d = _get(m.group(1), f"https://dlive.sx/stream/stream-{cid}.php").text
    b = re.search(r"atob\('([A-Za-z0-9+/=]+)'\)", d)
    if not b:
        raise ValueError("m3u8 base64 bulunamadi")
    return base64.b64decode(b.group(1)).decode()


def get_m3u8(cid):
    now = time.time()
    with CACHE_LOCK:
        c = CACHE.get(cid)
        if c and now - c["time"] < M3U8_TTL:
            return c["url"]
    url = extract_m3u8(cid)
    with CACHE_LOCK:
        CACHE[cid] = {"url": url, "time": now}
    return url


def drop_cache(cid):
    with CACHE_LOCK:
        CACHE.pop(cid, None)


def rewrite(content, source_url, cid):
    out = []
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if "URI=" in s:
                s = re.sub(r'URI=["\']([^"\']+)["\']',
                           lambda m: 'URI="/segment?channel=%d&url=%s"'
                           % (cid, quote(urljoin(source_url, m.group(1)), safe="")),
                           s)
            out.append(s)
        else:
            out.append("/segment?channel=%d&url=%s"
                       % (cid, quote(urljoin(source_url, s), safe="")))
    return "\n".join(out)


def upstream_get(url):
    s = requests.Session()
    s.headers.update(STREAM_HEADERS)
    return s.get(url, timeout=15)


def channel_list():
    """Gomulu snapshot + ana sayfa programi (birlesim). Yeni kanallar boyle eklenir."""
    snap = os.path.join(os.path.dirname(__file__), "channels.json")
    try:
        union = {c["id"]: c["name"] for c in json.load(open(snap, encoding="utf-8"))}
    except Exception:
        union = {}
    now = time.time()
    with CACHE_LOCK:
        c = CACHE.get("chanlist")
        if c and now - c["time"] < CHANLIST_TTL:
            for ch in c["url"]:
                union.setdefault(ch["id"], ch["name"])
            return [{"id": i, "name": union[i]} for i in sorted(union)]
    try:
        d = _get("https://dlive.sx/", "https://dlive.sx/").text
        fresh = []
        for cid, name in re.findall(r'href="/(?:watch\.php\?id=([0-9]+))"[^>]*title="([^"]+)"', d):
            cid = int(cid)
            if cid:
                fresh.append({"id": cid,
                              "name": html.unescape(html.unescape(name)).strip()})
        if len(fresh) > 50:
            with CACHE_LOCK:
                CACHE["chanlist"] = {"url": fresh, "time": now}
            for ch in fresh:
                union.setdefault(ch["id"], ch["name"])
    except Exception:
        pass
    return [{"id": i, "name": union[i]} for i in sorted(union)]


@app.route("/playlist.m3u")
@app.route("/playlist.m3u8")
def playlist():
    base = request.url_root.rstrip("/")
    lines = ["#EXTM3U"]
    for c in channel_list():
        lines.append("#EXTINF:-1,%s" % c["name"])
        lines.append("%s/live/%d.m3u8" % (base, c["id"]))
    return Response("\n".join(lines), content_type="application/vnd.apple.mpegurl")


@app.route("/live/<int:cid>.m3u8")
def live(cid):
    try:
        url = get_m3u8(cid)
    except Exception as e:
        return Response("#EXTM3U\n#EXT-X-ENDLIST", status=404,
                        content_type="application/vnd.apple.mpegurl")
    for attempt in range(2):
        try:
            r = upstream_get(url)
        except requests.RequestException:
            return Response("#EXTM3U\n#EXT-X-ENDLIST", status=502,
                            content_type="application/vnd.apple.mpegurl")
        if r.status_code in (401, 403) and attempt == 0:
            drop_cache(cid)
            try:
                url = extract_m3u8(cid)
                with CACHE_LOCK:
                    CACHE[cid] = {"url": url, "time": time.time()}
                continue
            except Exception:
                break
        if r.status_code != 200:
            drop_cache(cid)
            return Response("#EXTM3U\n#EXT-X-ENDLIST", status=r.status_code,
                            content_type="application/vnd.apple.mpegurl")
        return Response(rewrite(r.text, url, cid), content_type="application/vnd.apple.mpegurl")
    return Response("#EXTM3U\n#EXT-X-ENDLIST", status=502,
                    content_type="application/vnd.apple.mpegurl")


@app.route("/segment")
def segment():
    target = request.args.get("url")
    cid = int(request.args.get("channel", "0"))
    if not target:
        return "URL eksik", 400
    try:
        s = requests.Session()
        s.headers.update(STREAM_HEADERS)
        r = s.get(target, stream=True, timeout=15)
    except requests.RequestException as e:
        return "Segment hatasi: %s" % e, 502
    if r.status_code != 200:
        if r.status_code in (401, 403):
            drop_cache(cid)
        return "Segment error %d" % r.status_code, r.status_code
    ctype = (r.headers.get("Content-Type") or "").lower()
    if ".m3u8" in target.lower() or "mpegurl" in ctype:
        return Response(rewrite(r.text, target, cid),
                        content_type="application/vnd.apple.mpegurl")

    def gen():
        for chunk in r.iter_content(chunk_size=64 * 1024):
            yield chunk
    return Response(gen(), content_type=r.headers.get("Content-Type", "video/mp2t"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5757)))

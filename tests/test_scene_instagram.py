"""A venue's Instagram profile picture: og:image, kept 30 days, failures backed off."""
import io

from PIL import Image

from twiddle.scene import cache, instagram

PAGE = ('<meta property="og:image" content="https://scontent.cdninstagram.com/v/p.jpg'
        '?stp=dst-jpg_s100x100&amp;oe=ABC" />')


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (10, 200, 10)).save(buf, "JPEG")
    return buf.getvalue()


class Resp:
    def __init__(self, text="", content=b""):
        self.text, self.content = text, content


def test_picture_url_unescapes_the_signed_link():
    assert instagram.picture_url(PAGE).endswith("s100x100&oe=ABC")
    assert instagram.picture_url("<html>login</html>") is None


def test_the_picture_is_fetched_once_then_read_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []

    def get(url, **kw):
        calls.append(url)
        return Resp(text=PAGE) if "instagram.com/" in url and "cdn" not in url else Resp(content=_jpeg())
    monkeypatch.setattr(instagram.requests, "get", get)
    assert instagram.profile_picture("ivyroom").size == (100, 100)
    assert instagram.profile_picture("ivyroom").getpixel((50, 50))[1] > 150
    assert len(calls) == 2                     # the page and the image, once


def test_a_failure_is_not_retried_for_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(instagram.requests, "get",
                        lambda url, **kw: calls.append(url) or Resp(text="<html>login</html>"))
    assert instagram.profile_picture("nobody") is None
    assert instagram.profile_picture("nobody") is None
    assert len(calls) == 1

"""Animated profile covers — a Travel+ perk, and only for the cover.

An animated avatar would move in every list on every screen; a cover is one
image on one screen, which is where motion is a flourish rather than noise.
Subscription is checked on the server (see identity router): the client can
send anything.
"""

import io
from uuid import uuid4

import pytest
from PIL import Image

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.application import media as identity_media


def _animated_gif(frames: int = 3, size: tuple[int, int] = (64, 48)) -> bytes:
    """Frames have to actually differ — Pillow collapses identical ones, and
    the result would be a single-frame GIF that never exercises this path."""
    buffer = io.BytesIO()
    images = []
    for index in range(frames):
        frame = Image.new("RGB", size, color=(20, 40, 60))
        offset = index % max(1, size[0] - 2)
        frame.paste((240, 240, 240), (offset, 0, offset + 2, size[1]))
        images.append(frame.convert("P"))
    images[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=80,
        loop=0,
    )
    return buffer.getvalue()


def _still_png(size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(41, 151, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_animated_cover_is_stored_unflattened_for_subscribers(tmp_path, monkeypatch):
    # _MEDIA_ROOT читается при импорте, поэтому setenv тут не поможет —
    # подменяем сам модульный путь, как это уже делают тесты медиа маршрутов.
    monkeypatch.setattr(identity_media, "_MEDIA_ROOT", tmp_path)
    raw = _animated_gif(frames=4)

    saved = identity_media.save_profile_image_bytes(
        raw, user_id=uuid4(), kind="cover", allow_animated=True
    )

    assert saved.content_type == "image/gif"
    assert saved.storage_key.endswith(".gif")
    stored = (tmp_path / saved.storage_key).read_bytes()
    # Byte-identical: re-encoding would flatten it to one frame, which is the
    # opposite of what was asked for.
    assert stored == raw
    with Image.open(io.BytesIO(stored)) as reopened:
        assert reopened.n_frames == 4


def test_without_travel_plus_a_gif_is_refused(tmp_path, monkeypatch):
    # _MEDIA_ROOT читается при импорте, поэтому setenv тут не поможет —
    # подменяем сам модульный путь, как это уже делают тесты медиа маршрутов.
    monkeypatch.setattr(identity_media, "_MEDIA_ROOT", tmp_path)

    with pytest.raises(AppError) as exc:
        identity_media.save_profile_image_bytes(
            _animated_gif(), user_id=uuid4(), kind="cover", allow_animated=False
        )
    assert exc.value.code == "invalid_image"


def test_an_animated_avatar_is_refused_even_with_travel_plus(tmp_path, monkeypatch):
    """The perk is the cover, not every image on the profile."""
    # _MEDIA_ROOT читается при импорте, поэтому setenv тут не поможет —
    # подменяем сам модульный путь, как это уже делают тесты медиа маршрутов.
    monkeypatch.setattr(identity_media, "_MEDIA_ROOT", tmp_path)

    with pytest.raises(AppError) as exc:
        identity_media.save_profile_image_bytes(
            _animated_gif(), user_id=uuid4(), kind="avatar", allow_animated=True
        )
    assert exc.value.code == "invalid_image"


def test_too_many_frames_is_refused(tmp_path, monkeypatch):
    # _MEDIA_ROOT читается при импорте, поэтому setenv тут не поможет —
    # подменяем сам модульный путь, как это уже делают тесты медиа маршрутов.
    monkeypatch.setattr(identity_media, "_MEDIA_ROOT", tmp_path)

    with pytest.raises(AppError) as exc:
        identity_media.save_profile_image_bytes(
            _animated_gif(frames=400, size=(8, 8)),
            user_id=uuid4(),
            kind="cover",
            allow_animated=True,
        )
    assert "кадров" in exc.value.message


def test_a_still_image_still_goes_through_the_normal_path(tmp_path, monkeypatch):
    """Allowing animation must not change what happens to ordinary photos."""
    # _MEDIA_ROOT читается при импорте, поэтому setenv тут не поможет —
    # подменяем сам модульный путь, как это уже делают тесты медиа маршрутов.
    monkeypatch.setattr(identity_media, "_MEDIA_ROOT", tmp_path)

    saved = identity_media.save_profile_image_bytes(
        _still_png(), user_id=uuid4(), kind="cover", allow_animated=True
    )

    assert saved.content_type != "image/gif"
    assert not saved.storage_key.endswith(".gif")

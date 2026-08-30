"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import os
import shutil
import uuid
from typing import cast, Any
from loguru import logger as log
from PIL import Image

from src.backend.DeckManagement.HelperMethods import is_video, is_image, sha256, file_in_dir, download_file, is_svg
from src.backend import settings_store

import globals as gl


class AssetManagerBackend(list[Any]):
    # Use the settings-store path so this class and the store cannot name different files.
    # The data path is stable before this module imports.
    JSON_PATH = settings_store.ASSET_LIBRARY.path()
    def __init__(self) -> None:
        self.load_json()

        self.fill_missing_data()

        self.remove_invalid_data()

    def load_json(self) -> None:
        # An unreadable index must not stop startup; the store quarantines corruption and returns an empty library.
        # A missing index also starts empty.
        self.clear()
        self.extend(settings_store.get().read(settings_store.ASSET_LIBRARY))

    def save_json(self) -> None:
        settings_store.get().write(settings_store.ASSET_LIBRARY, list(self))

    def add(self, asset_path: str, licence_name: str | None = None, licence_url: str | None = None, author: str | None = None) -> str | None:
        if not os.path.exists(asset_path):
            log.warning(f"File {asset_path} not found.")
            return None
        
        
        try:
            hash = sha256(asset_path)
        except OSError as e:
            # Keep the import worker alive when this asset cannot be read; callers handle None.
            log.opt(exception=True).warning(f"Could not read asset {asset_path}: {e}")
            return None

        existing = self.get_by_sha256(hash)
        if existing is not None:
            #TODO: It is possible that the some image has the same sha but not the name because it got renamed
            log.warning(f"Tried to add already existing asset. Ignoring. File: {asset_path}")
            return cast(str | None, existing["id"])

        # Reject undecodable imports before copying; existing assets stay because decode failures can be transient.
        # Reuse this image for video and SVG thumbnails.
        decoded = self._decode_for_import(asset_path)
        if decoded is None:
            log.warning(f"Refusing to import undecodable asset {asset_path}")
            return None

        # Always copy into internal data because deletion trusts internal-path.
        # copy_asset() handles same-file and name-collision cases.
        try:
            internal_path = self.copy_asset(asset_path)
        except Exception as e:
            # Fail only this asset if copying fails, and keep the import worker alive.
            log.opt(exception=True).warning(f"Could not import asset {asset_path}: {e}")
            return None

        # Video and SVG thumbnail failures stay None so fill_missing_thumbnails retries them.
        thumbnail_path: str | None = internal_path

        if is_video(asset_path):
            thumbnail_path = self.save_thumbnail(asset_path, hash, image=decoded)

        if is_svg(asset_path):
            thumbnail_path = self.save_thumbnail(asset_path, hash, image=decoded)


        asset: dict[str, Any] = {
            "name": os.path.splitext(os.path.basename(asset_path))[0],
            "original-path": asset_path,
            "internal-path": internal_path,
            "sha256": hash,
            "id": self.create_unique_uuid(),
            "license": {
                "name": licence_name,
                "url": licence_url,
                "author": author
            },
            "thumbnail": thumbnail_path
        }
        self.append(asset)

        self.save_json()

        return cast(str | None, asset["id"])

    def _decode_for_import(self, path: str) -> Image.Image | None:
        # generate_thumbnail returns a tagged placeholder instead of raising on decode failure.
        # Reject that placeholder and return the reusable image otherwise.
        thumbnail = gl.media_manager.generate_thumbnail(path)
        if thumbnail.info.get("sc_broken"):
            return None
        return cast("Image.Image | None", thumbnail)

    def save_thumbnail(self, asset_path: str, asset_hash: str,
                       image: Image.Image | None = None) -> str | None:
        thumbnail_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "thumbnails", f"{asset_hash}.png")

        if os.path.exists(thumbnail_path):
            return thumbnail_path
        if not (is_video(asset_path) or is_svg(asset_path)):
            return asset_path
        
        os.makedirs(os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "thumbnails"), exist_ok=True)

        # Keep thumbnail failures local; None makes startup retry transient video or SVG failures.
        # add() supplies its decoded image so imports do not decode twice.
        try:
            thumbnail = image if image is not None else gl.media_manager.generate_thumbnail(asset_path)
            if thumbnail.info.get("sc_broken"):
                # Do not save the logged placeholder; None keeps the thumbnail retryable.
                return None
            gl.media_manager.save_image_atomic(thumbnail, thumbnail_path)
        except Exception as e:
            log.opt(exception=True).warning(f"Could not create thumbnail for {asset_path}: {e}")
            return None

        return thumbnail_path
    
    def remove_asset_by_id(self, id: str) -> None:
        asset = self.get_by_id(id)
        if asset is None:
            return
        
        internal_path = asset["internal-path"]

        if gl.page_manager is not None:
            gl.page_manager.remove_asset_from_all_pages(internal_path)

        # Guard the delete. A broken asset whose file is already gone must
        # still lose its entry, and must not raise out of the UI.
        try:
            if internal_path is not None and os.path.exists(internal_path):
                os.remove(internal_path)
        except OSError as e:
            log.opt(exception=True).warning(f"Could not delete asset file {internal_path}: {e}")

        self.remove(asset)
        self.save_json()
        
        
    def copy_asset(self, asset_path: str) -> str:
        file_name = os.path.basename(asset_path)
        dst_path = None
        if not file_in_dir(file_name, os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets")):
            dst_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets", file_name)
        else:
            log.warning(f"File with same name already exists but sha256 does not match, renaming: {asset_path}")
            original_base, ext = os.path.splitext(os.path.basename(asset_path))
            index = 2
            while file_in_dir(file_name, os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets")):
                file_name = f"{original_base}-{str(index).zfill(2)}{ext}"
                index += 1
            dst_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets", file_name)

        if asset_path == dst_path:
            return asset_path

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy(asset_path, dst_path)
        except shutil.SameFileError:
            log.warning(f"File already exists: {dst_path}")
        return dst_path
    
    def create_unique_uuid(self) -> str:
        id = str(uuid.uuid4())
        if self.has_by_id(id):
            log.warning("Congratulations, you already have an asset with this id. This is very rare.")
            return self.create_unique_uuid()
        return id

    def has_by_name(self, name: str) -> bool:
        return self.get_by_name(name) is not None
            
    def has_by_sha256(self, sha256: str) -> bool:
        return self.get_by_sha256(sha256) is not None

    def has_by_id(self, id: str) -> bool:
        return self.get_by_id(id) is not None
    
    def has_by_internal_path(self, internal_path: str) -> bool:
        return self.get_by_internal_path(internal_path) is not None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        for asset in self:
            if asset["name"] == name:
                return cast(dict[str, Any] | None, asset)
        return None

    def get_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        for asset in self:
            if asset["sha256"] == sha256:
                return cast(dict[str, Any] | None, asset)
        return None

    def get_by_id(self, id: str) -> dict[str, Any] | None:
        for asset in self:
            if asset["id"] == id:
                return cast(dict[str, Any] | None, asset)
        return None

    def get_by_internal_path(self, internal_path: str) -> dict[str, Any] | None:
        for asset in self:
            if asset["internal-path"] == internal_path:
                return cast(dict[str, Any] | None, asset)
        return None

    def get_all(self) -> list[Any]:
        return self
    
    def fill_missing_data(self) -> None:
        def fill_missing_folders() -> None:
            os.makedirs(os.path.join(gl.DATA_PATH, "Assets", "thumbnails"), exist_ok=True)

        def fill_missing_thumbnails() -> None:
            for asset in self:
                # Test for None before os.path.exists() so a failed thumbnail does not stop startup.
                if asset.get("thumbnail") is not None:
                    if os.path.exists(asset["thumbnail"]):
                        continue

                # Keep one invalid entry from stopping the batch or startup.
                # None makes the next startup retry it.
                try:
                    thumbnail_path = self.save_thumbnail(asset["internal-path"], asset["sha256"])
                except Exception as e:
                    log.opt(exception=True).warning(
                        f"Could not restore thumbnail for {asset.get('internal-path')}: {e}")
                    thumbnail_path = None

                asset["thumbnail"] = thumbnail_path

        
        fill_missing_folders()
        fill_missing_thumbnails()

        self.save_json()

    def remove_invalid_data(self) -> None:
        # Iterate over a copy because removing from self skips the next item.
        # Treat a null internal path as missing before os.path.exists().
        for asset in list(self):
            internal_path = asset.get("internal-path")
            if internal_path is None or not os.path.exists(internal_path):
                self.remove(asset)
        self.save_json()

    def _alert_on_main(self, window: "Gtk.Window | None", message: str, detail: str) -> None:
        # Create the dialog in the idle callback because file drops use a worker and GTK requires the main thread.
        # Pass the window so the modal dialog has a parent.
        def show() -> None:
            dial = Gtk.AlertDialog(message=message, detail=detail, modal=True)
            dial.show(window)
        GLib.idle_add(show)

    def add_custom_media_set_by_ui(self, url: str | None, path: str | None) -> str | None:
        window: Gtk.Window | None = gl.app.main_win if gl.app is not None else None
        if gl.store is not None:
            window = gl.store

        if path is None and url is not None:
            extension = os.path.splitext(url)[1].lower().replace(".", "")
            if extension not in (set(gl.video_extensions) | set(gl.image_extensions) | set(gl.svg_extensions)):

                self._alert_on_main(
                    window,
                    message="The image is invalid.",
                    detail="You can only use urls directly pointing to images (not directly from Google).",
                )
                return None

            os.makedirs(os.path.join(gl.DATA_PATH, "cache", "downloads"), exist_ok=True)
            # Catch network and HTTP failures so a bad URL reports an error instead of ending the import worker.
            # KeyGrid can also call this path on the GTK main thread.
            try:
                path = download_file(url=url, path=os.path.join(gl.DATA_PATH, "cache", "downloads"))
            except Exception as e:
                log.opt(exception=True).error(f"Could not download asset from {url}: {e}")
                self._alert_on_main(
                    window,
                    message="The download failed.",
                    detail="The image or video could not be downloaded. Check the url and your connection.",
                )
                return None

        if path is None:
            return None
        if not os.path.exists(path):
            return None
        if not is_video(path) and not is_image(path) and not is_svg(path):
            self._alert_on_main(
                window,
                message="No valid image or video.",
                detail="Only images and videos are supported.",
            )
            return None
        asset_id = gl.asset_manager_backend.add(asset_path=path)
        if asset_id is None:
            # Report unreadable, uncopyable, or undecodable drops because they otherwise have no UI result.
            self._alert_on_main(
                window,
                message="No valid image or video.",
                detail="Only images and videos are supported. The file may also be corrupt or unreadable.",
            )
            return None

        asset = self.get_by_id(asset_id)
        if asset is None:
            return None

        if gl.asset_manager is not None:
            gl.asset_manager.asset_chooser.custom_asset_chooser.add_asset(asset)

        return cast("str | None", asset.get("internal-path"))

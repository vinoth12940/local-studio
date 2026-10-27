"""Static files the page loads at start-up (frontend/ ES modules), added in the refactor's P2 split.

Every response says Cache-Control: no-cache, like index.html: after an update the app window must revalidate, never run a
new index.html against old modules. ETag/Last-Modified still make the revalidation cheap. JavaScript modules are always
served as text/javascript, whatever the system's mimetypes table says (a browser refuses a module with another type).
"""
from fastapi.staticfiles import StaticFiles


class FreshStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        if str(full_path).endswith((".mjs", ".js")):
            response.headers["Content-Type"] = "text/javascript; charset=utf-8"
        return response


class MediaStaticFiles(StaticFiles):
    """The image gallery: generated PNGs (unique names) and their settings sidecars, shown inline and cached privately."""
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "private, max-age=86400"
        raster = str(full_path).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))
        response.headers["Content-Disposition"] = "inline" if raster else "attachment"
        return response

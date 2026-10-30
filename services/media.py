import os
import cloudinary
import cloudinary.uploader


def configured() -> bool:
    return all(os.environ.get(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))


def _config():
    cloudinary.config(cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"], api_key=os.environ["CLOUDINARY_API_KEY"],
                      api_secret=os.environ["CLOUDINARY_API_SECRET"], secure=True)


def upload(source, public_id: str = None, folder: str = "rafdecor/uploads") -> dict:
    _config()
    res = cloudinary.uploader.upload(source, folder=folder, public_id=public_id, overwrite=False,
                                     unique_filename=public_id is None, resource_type="image")
    return {"url": res["secure_url"], "public_id": res["public_id"], "bytes": res.get("bytes"), "width": res.get("width"), "height": res.get("height")}


def destroy(public_id: str):
    _config()
    cloudinary.uploader.destroy(public_id, invalidate=True)

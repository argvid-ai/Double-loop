"""Keep media provenance in durable records, never duplicate transient image bytes."""
import hashlib


def without_image_bytes(value):
    if isinstance(value, list):
        return [without_image_bytes(v) for v in value]
    if not isinstance(value, dict):
        return value
    encoded = None
    if value.get("type") == "image" and isinstance(value.get("data"), str):
        encoded = value["data"]
    elif value.get("type") == "image_url":
        url = value.get("image_url", {}).get("url", "")
        if url.startswith("data:image/"):
            encoded = url
    if encoded is not None:
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        return {
            "type": "text",
            "text": "[Transient image omitted from stored history; "
                    f"encoded_sha256={fingerprint}. Reread the referenced media when needed.]",
        }
    return {k: without_image_bytes(v) for k, v in value.items()}

"""Publish a verified snapshot with immutable content, and restore a previous version."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

from check_public_json import check

MUTABLE_CACHE = "no-store"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
VERSION = re.compile(r"[0-9]{8}T[0-9]{12}Z")


def encode(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def atomic_write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + uuid4().hex + ".tmp")
    try:
        temp.write_bytes(body)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def object_key(digest):
    return f"objects/{digest}.json"


def materialize(root, staging, legacy):
    """Write only content that does not already exist in the published object store."""
    current = {**legacy, "schema_version": 2, "districts": []}
    objects = set()

    def add(body):
        digest = sha256(body).hexdigest()
        path = root / object_key(digest)
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
            atomic_write(path, body)
        objects.add(digest)
        return digest

    release = staging / legacy["release"]
    for district in legacy["districts"]:
        code = district["code"]
        directory = release / "transactions" / code
        history = {file.stem: add(file.read_bytes()) for file in sorted(directory.glob("*.json"))}
        current["districts"].append({
            **district,
            "complexes": add((release / "complexes" / f"{code}.json").read_bytes()),
            "latest": add((release / "latest-trades" / f"{code}.json").read_bytes()),
            "history": add(encode(history)),
        })
    return current, objects


class R2:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    @classmethod
    def configured(cls):
        if os.environ.get("JIPFIT_PUBLISH_R2") != "1":
            return None
        import boto3
        from botocore.config import Config
        endpoint = os.environ["R2_ENDPOINT"]
        if not endpoint.startswith("https://"):
            raise ValueError("R2_ENDPOINT must use HTTPS")
        client = boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                              aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                              aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                              config=Config(retries={"mode": "standard", "max_attempts": 4}))
        return cls(client, os.environ["R2_BUCKET"])

    def get(self, key):
        from botocore.exceptions import ClientError
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                raise
            return None, None
        with response["Body"] as body:
            return json.load(body), response["ETag"]

    def put(self, key, data, cache, **condition):
        return self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                      ContentType="application/json", CacheControl=cache, **condition)

    def immutable(self, key, data):
        from botocore.exceptions import ClientError
        try:
            self.put(key, data, IMMUTABLE_CACHE, IfNoneMatch="*")
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise

    def publish(self, root, manifest, objects):
        previous, etag = self.get("manifest.json")
        if same_snapshot(previous, manifest):
            return previous
        known = set()
        if previous and previous["schema_version"] == 2:
            for district in previous["districts"]:
                known.update(district[field] for field in ("complexes", "latest", "history"))
                history, _ = self.get(object_key(district["history"]))
                if history is None:
                    raise ValueError("Current R2 publication is incomplete")
                known.update(history.values())
        for digest in sorted(objects - known):
            self.immutable(object_key(digest), (root / object_key(digest)).read_bytes())
        body = encode(manifest)
        self.immutable(manifest["release"] + "/manifest.json", body)
        self.put("manifest.json", body, MUTABLE_CACHE,
                 **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}))
        return manifest


def same_snapshot(first, second):
    return first is not None and all(first.get(key) == second[key]
                                     for key in ("schema_version", "districts", "totals"))


def publish(root, remote=None):
    from export_public_json import build_release
    root.mkdir(parents=True, exist_ok=True)
    current_path = root / "manifest.json"
    with TemporaryDirectory() as temp:
        staging = Path(temp)
        legacy = build_release(staging)
        manifest, objects = materialize(root, staging, legacy)
    previous = json.loads(current_path.read_bytes()) if current_path.is_file() else None
    if same_snapshot(previous, manifest):
        manifest = previous
    check(root, manifest=manifest)
    if remote:
        manifest = remote.publish(root, manifest, objects)
    if manifest != previous:
        body = encode(manifest)
        atomic_write(root / manifest["release"] / "manifest.json", body)
        atomic_write(current_path, body)
    print(f"Verified snapshot: {manifest['release']} {manifest['totals']}", flush=True)
    return manifest


def rollback(root, version, remote=None):
    if not VERSION.fullmatch(version):
        raise ValueError("Invalid release version")
    body = (root / "releases" / version / "manifest.json").read_bytes()
    manifest = json.loads(body)
    check(root, manifest=manifest)
    if remote:
        archived, _ = remote.get(manifest["release"] + "/manifest.json")
        if archived != manifest:
            raise ValueError("The requested release was not published to R2")
        _, etag = remote.get("manifest.json")
        remote.put("manifest.json", body, MUTABLE_CACHE,
                   **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}))
    atomic_write(root / "manifest.json", body)

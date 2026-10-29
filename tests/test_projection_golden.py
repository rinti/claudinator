import hashlib
import json
from pathlib import Path

from claudinator.archive import Archive, Config
from claudinator.record import PROJECTION_VERSION
from helpers import projection_dump

CORPUS = Path(__file__).parent / "fixtures" / "corpus"

GOLDEN = {
    2: "6f14f11974a85ce9aea3ecccadaabc51287a00690227347cce75b19bf3743cb2",
}


def test_projection_of_the_fixture_corpus_matches_the_recorded_digest(tmp_path):
    cfg = Config(db=tmp_path / "golden.db", claude_dir=CORPUS / "claude", desktop_dir=CORPUS / "desktop")
    with Archive.open(cfg) as archive:
        assert archive.sweep().errors == ()
        dump = projection_dump(archive, CORPUS)
    digest = hashlib.sha256(json.dumps(dump, ensure_ascii=False).encode()).hexdigest()
    assert PROJECTION_VERSION in GOLDEN, f"record GOLDEN[{PROJECTION_VERSION}] = {digest!r}"
    assert digest == GOLDEN[PROJECTION_VERSION], (
        f"projection output changed: bump PROJECTION_VERSION and record the new digest {digest!r}"
    )

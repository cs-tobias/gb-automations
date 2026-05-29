from gb_automations.clients.notion import read_first_file_url


def _files_prop(entries):
    return {"properties": {"Thumbnail": {"type": "files", "files": entries}}}


def test_read_first_file_url_notion_hosted():
    page = _files_prop(
        [{"type": "file", "file": {"url": "https://s3.example/signed?x=1"}}]
    )
    assert read_first_file_url(page, "Thumbnail") == "https://s3.example/signed?x=1"


def test_read_first_file_url_external():
    page = _files_prop(
        [{"type": "external", "external": {"url": "https://cdn.example/ref.jpg"}}]
    )
    assert read_first_file_url(page, "Thumbnail") == "https://cdn.example/ref.jpg"


def test_read_first_file_url_picks_first():
    page = _files_prop(
        [
            {"type": "external", "external": {"url": "https://first"}},
            {"type": "external", "external": {"url": "https://second"}},
        ]
    )
    assert read_first_file_url(page, "Thumbnail") == "https://first"


def test_read_first_file_url_empty_or_missing():
    assert read_first_file_url(_files_prop([]), "Thumbnail") is None
    assert read_first_file_url({"properties": {}}, "Thumbnail") is None
    assert read_first_file_url({}, "Thumbnail") is None

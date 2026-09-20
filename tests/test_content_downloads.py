"""Real download routes over isolated accounts and files; providers are mocked.

The video payload below is an attachment fixture, not a playable-video or model
generation check. PDF and Word responses are parsed using independent readers.
"""
from io import BytesIO
from urllib.parse import unquote

from docx import Document
from pypdf import PdfReader
import pytest

from app.store import dumps, now, uid
from test_account_isolation import another_client, register
from test_workflow import generated, rig, seed


TITLE = 'Download fixture 下载测试'
EXPLANATION = 'PRIVATE_ANSWER_EXPLANATION_49270: compare the evidence first.'
MIME_TYPES = {
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'md': 'text/markdown; charset=utf-8',
}


@pytest.fixture
def downloadable(rig):
    client, app, wire = rig
    course, _ = seed(client)
    cid = generated(client, course)
    asset = client.get(f'/api/contents/{cid}').json()['asset']
    asset['title'] = TITLE
    asset['questions'][0]['explanation'] = EXPLANATION
    revised = client.post(f'/api/contents/{cid}/review', json={
        'version': 1, 'action': 'save', 'asset': asset,
    })
    assert revised.status_code == 200, revised.text
    assert revised.json()['version'] == 2
    return client, app, wire, cid


def exported_text(response, format):
    if format == 'pdf':
        return '\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    if format == 'docx':
        return '\n'.join(paragraph.text for paragraph in Document(BytesIO(response.content)).paragraphs)
    return response.text


@pytest.mark.parametrize('format', ['pdf', 'docx', 'md'])
@pytest.mark.parametrize('include_answers', [True, False])
def test_saved_version_downloads_are_files_and_honor_answer_selection(downloadable, format, include_answers):
    client, app, wire, cid = downloadable
    before = app.state.store.one('SELECT * FROM contents WHERE id=?', (cid,))
    calls = wire.calls
    response = client.get(f'/api/contents/{cid}/export', params={
        'format': format, 'include_answers': include_answers, 'language': 'en', 'version': 2,
    })
    assert response.status_code == 200, response.text[:200]
    assert response.headers['content-type'] == MIME_TYPES[format]
    disposition = response.headers['content-disposition']
    assert disposition.startswith('attachment;')
    assert '.' + format in disposition
    if format in ('pdf', 'docx'):
        assert response.headers['x-content-version'] == '2'
        assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == f'{TITLE}-v2.{format}'
        assert response.content.startswith(b'%PDF' if format == 'pdf' else b'PK')
    text = exported_text(response, format)
    assert TITLE in text
    assert '检索的作用' in text
    assert '提供参考' in text  # The learner still receives the question and its options.
    assert (EXPLANATION in text) is include_answers
    if not include_answers:
        assert 'Answers and explanations' not in text
        assert '参考答案：' not in text
    assert wire.calls == calls
    assert app.state.store.one('SELECT * FROM contents WHERE id=?', (cid,)) == before


@pytest.mark.parametrize('format', ['pdf', 'docx'])
def test_export_language_changes_document_labels_without_translating_saved_content(downloadable, format):
    client, _, _, cid = downloadable
    english = client.get(f'/api/contents/{cid}/export', params={'format':format,'language':'en','version':2})
    chinese = client.get(f'/api/contents/{cid}/export', params={'format':format,'language':'zh','version':2})
    assert english.status_code == chinese.status_code == 200
    en_text, zh_text = exported_text(english,format), exported_text(chinese,format)
    assert TITLE in en_text and TITLE in zh_text
    assert EXPLANATION in en_text and EXPLANATION in zh_text
    assert 'Answers and explanations' in en_text
    assert '答案与解析' in zh_text


@pytest.mark.parametrize('format', ['pdf', 'docx', 'md'])
@pytest.mark.parametrize('version', [1, 3])
def test_export_rejects_a_stale_or_unavailable_version(downloadable, format, version):
    client, _, wire, cid = downloadable
    calls = wire.calls
    response = client.get(f'/api/contents/{cid}/export', params={'format':format,'version':version})
    assert response.status_code == 409
    assert 'content-disposition' not in response.headers
    assert wire.calls == calls


@pytest.mark.parametrize('params', [{'format':'html'}, {'format':'pdf','language':'fr'}])
def test_unsupported_export_options_fail_as_a_request_error(downloadable, params):
    client, _, _, cid = downloadable
    response = client.get(f'/api/contents/{cid}/export', params=params)
    assert response.status_code == 400
    assert 'content-disposition' not in response.headers


def media_record(app, cid, path, *, kind='video', mime='video/mp4'):
    mid = uid()
    app.state.store.execute('INSERT INTO media VALUES(?,?,?,?,?,?,?,?,?)', (
        mid, cid, 2, kind, str(path), mime, 'draft', dumps({}), now(),
    ))
    return mid


def dummy_video(app, cid):
    directory = app.state.settings.data_dir / 'media'
    directory.mkdir(exist_ok=True)
    path = directory / 'future-video.mp4'
    data = b'\x00\x00\x00\x18ftypmp42ATTACHMENT_FIXTURE_ONLY'
    path.write_bytes(data)
    return media_record(app, cid, path.name), data


def test_future_video_download_is_an_attachment_and_existing_inline_route_still_works(downloadable):
    client, _, wire, cid = downloadable
    app = client.app
    mid, data = dummy_video(app, cid)
    calls = wire.calls
    download = client.get(f'/api/media/{mid}/download')
    assert download.status_code == 200
    assert download.content == data
    assert download.headers['content-type'] == 'video/mp4'
    assert download.headers['content-disposition'] == 'attachment; filename="future-video.mp4"'
    inline = client.get(f'/api/media/{mid}/file')
    assert inline.status_code == 200 and inline.content == data
    assert 'attachment' not in inline.headers.get('content-disposition', '')
    assert wire.calls == calls


@pytest.mark.parametrize('kind,mime,filename,data', [
    ('audio','audio/mpeg','lesson.mp3',b'ID3AUDIO_ATTACHMENT_FIXTURE'),
    ('image','image/png','diagram.png',b'\x89PNG\r\nIMAGE_ATTACHMENT_FIXTURE'),
])
def test_existing_media_kinds_share_the_attachment_download_contract(downloadable, kind, mime, filename, data):
    client, app, _, cid = downloadable
    directory = app.state.settings.data_dir / 'media'
    directory.mkdir(exist_ok=True)
    (directory / filename).write_bytes(data)
    mid = media_record(app,cid,filename,kind=kind,mime=mime)
    response = client.get(f'/api/media/{mid}/download')
    assert response.status_code == 200 and response.content == data
    assert response.headers['content-type'] == mime
    assert response.headers['content-disposition'] == f'attachment; filename="{filename}"'


def test_export_and_media_download_reject_other_accounts_and_anonymous_clients(downloadable):
    owner, app, wire, cid = downloadable
    mid, _ = dummy_video(app,cid)
    calls = wire.calls
    urls = [f'/api/contents/{cid}/export?format={format}&version=2' for format in ('pdf','docx','md')]
    urls += [f'/api/media/{mid}/download', f'/api/media/{mid}/file']
    with another_client(app,owner) as other:
        for url in urls:
            response = other.get(url)
            assert response.status_code == 401, (url,response.text)
            assert 'content-disposition' not in response.headers
        register(other,'download-other@example.test','Other account')
        for url in urls:
            response = other.get(url)
            assert response.status_code == 404, (url,response.text)
            assert EXPLANATION not in response.text
            assert TITLE not in response.text
            assert 'content-disposition' not in response.headers
    assert wire.calls == calls
    assert owner.get(f'/api/media/{mid}/download').status_code == 200


@pytest.mark.parametrize('mode', ['parent-relative', 'absolute', 'symlink', 'missing', 'directory'])
@pytest.mark.parametrize('endpoint', ['download', 'file'])
def test_media_paths_must_resolve_to_an_existing_file_inside_the_media_directory(downloadable, mode, endpoint):
    client, app, _, cid = downloadable
    directory = app.state.settings.data_dir / 'media'
    directory.mkdir(exist_ok=True)
    outside = app.state.settings.data_dir / 'outside.bin'
    outside.write_bytes(b'PRIVATE_OUTSIDE_MEDIA_DIRECTORY')
    if mode == 'symlink':
        (directory / 'escape.mp4').symlink_to(outside)
        path = 'escape.mp4'
    else:
        path = {'parent-relative':'../outside.bin','absolute':str(outside),'missing':'not-created.mp4','directory':'.'}[mode]
    mid = media_record(app,cid,path)
    response = client.get(f'/api/media/{mid}/{endpoint}')
    assert response.status_code == 404
    assert b'PRIVATE_OUTSIDE_MEDIA_DIRECTORY' not in response.content
    assert 'content-disposition' not in response.headers


def test_nested_media_files_are_downloadable_when_the_resolved_path_stays_inside(downloadable):
    client, app, _, cid = downloadable
    directory = app.state.settings.data_dir / 'media' / 'video'
    directory.mkdir(parents=True,exist_ok=True)
    (directory / 'lesson.mp4').write_bytes(b'NESTED_ATTACHMENT_FIXTURE')
    mid = media_record(app,cid,'video/lesson.mp4')
    response = client.get(f'/api/media/{mid}/download')
    assert response.status_code == 200
    assert response.content == b'NESTED_ATTACHMENT_FIXTURE'
    assert response.headers['content-disposition'] == 'attachment; filename="lesson.mp4"'

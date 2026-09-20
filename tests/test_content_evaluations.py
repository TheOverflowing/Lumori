import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.content_evaluations import current_evaluation, save_evaluation
from app.store import Conflict, Store, dumps, now
from test_workflow import rig, seed, generated


def metrics(score=4):
    return {'version':1,'correctness':score,'groundedness':4,'difficulty_match':3,
            'notes':'Useful material','question_difficulties':[]}


def standalone(tmp_path):
    store=Store(tmp_path)
    store.execute('INSERT INTO courses VALUES(?,?,?)',('course','Course',now()))
    store.execute('INSERT INTO contents VALUES(?,?,?,?,?,?,?,?,?)',
                  ('content','course','job',1,'draft','{}','[]','{}',now()))
    return store


def test_current_rating_is_an_atomic_editable_record_with_revision_history(tmp_path):
    store=standalone(tmp_path)
    assert current_evaluation(store,'content',1) is None
    with ThreadPoolExecutor(max_workers=5) as workers:
        results=list(workers.map(lambda _:save_evaluation(store,'content',1,metrics()),range(5)))
    assert len({item['id'] for item in results})==1
    assert store.one('SELECT count(*) n FROM evaluations')['n']==1
    assert store.one('SELECT count(*) n FROM evaluation_history')['n']==0
    result=save_evaluation(store,'content',1,metrics(5))
    assert result['created'] is False
    assert result['evaluation']['metrics']['correctness']==5
    assert store.one('SELECT count(*) n FROM evaluations')['n']==1
    assert json.loads(store.one('SELECT metrics FROM evaluation_history')['metrics'])['correctness']==4
    store.execute('UPDATE contents SET version=2 WHERE id=?',('content',))
    assert current_evaluation(store,'content',2) is None
    with pytest.raises(Conflict):save_evaluation(store,'content',1,metrics())


def test_legacy_duplicate_ratings_are_retained_and_latest_is_used(tmp_path):
    store=standalone(tmp_path)
    for id,score,stamp in [('old',2,'2026-01-01'),('latest',4,'2026-01-02')]:
        store.execute('INSERT INTO evaluations VALUES(?,?,?,?,?)',(id,'content',1,dumps(metrics(score)),stamp))
    assert current_evaluation(store,'content',1)['id']=='latest'
    result=save_evaluation(store,'content',1,metrics(5))
    assert result['id']=='latest'
    assert store.one('SELECT count(*) n FROM evaluations')['n']==2
    assert json.loads(store.one('SELECT metrics FROM evaluations WHERE id=?',('old',))['metrics'])['correctness']==2


def test_rating_route_round_trip_and_per_version_exports(rig):
    client,_,_=rig
    course,_=seed(client)
    cid=generated(client,course)
    assert client.get('/api/contents/'+cid).json()['evaluation'] is None
    first=client.post(f'/api/contents/{cid}/evaluations',json=metrics())
    assert first.status_code==201,first.text
    saved=client.get('/api/contents/'+cid).json()['evaluation']
    assert saved['metrics']['correctness']==4
    second=client.post(f'/api/contents/{cid}/evaluations',json=metrics(5))
    assert second.status_code==200,second.text
    assert second.json()['id']==first.json()['id']
    rows=list(csv.DictReader(io.StringIO(client.get('/api/evaluations/export').text.lstrip('\ufeff'))))
    assert len(rows)==1 and rows[0]['correctness']=='5'
    content=client.get('/api/contents/'+cid).json()
    asset=content['asset'];asset['title']='Updated title'
    response=client.post(f'/api/contents/{cid}/review',json={'version':1,'action':'save','asset':asset})
    assert response.status_code==200,response.text
    assert client.get('/api/contents/'+cid).json()['evaluation'] is None
    assert client.post(f'/api/contents/{cid}/evaluations',json=metrics()).status_code==409

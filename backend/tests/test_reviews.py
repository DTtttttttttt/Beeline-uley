"""Отзывы клиентов и рейтинг инженера (PLAN 9.1, блок 70).

День на двух бригадах: утром заявку «1» берёт бригада 1 (выезд в 09:30), в 09:10 она
выбывает, и заявка уходит бригаде 2 — та её и закрывает. Оценка обязана уйти бригаде 2.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db
from app.data import seed
from app.dictionaries import Outcome, ReviewSource
from app.main import app
from app.models import (
    Algorithm,
    CloseRequestEvent,
    EngineerUnavailableEvent,
    Rating,
    ReplanMode,
    Review,
    ReviewInput,
    Variant,
)
from app.planning import replan, reviews, service, variants
from app.planning.profiles import namespace
from app.routing import matrices, valhalla


@pytest.fixture
def day(mongo, make_request, make_engineer, make_dataset, make_travel, monkeypatch):
    """Утренний план, версия «бригада 1 выбыла», версия «заявка 1 выполнена»; все — в базе."""
    # Уникальный ключ отзыва — индекс базы: без него «одна оценка на заявку» не держится.
    db.ensure_indexes()
    requests = [
        make_request("1", windowEnd="18:00"),
        make_request("2", windowEnd="18:00"),
    ]
    dataset = make_dataset(
        requests,
        [make_engineer("brigade-1", shiftStart="09:30"), make_engineer("brigade-2")],
    )
    travel = make_travel(requests)
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    mongo.datasets.insert_one(
        {"_id": dataset.id, **dataset.model_dump(by_alias=True, exclude={"id"})}
    )

    def stored(plan):
        plan.created_at = datetime(2026, 10, 16, 9, tzinfo=UTC)
        mongo.plans.insert_one(service.to_document(plan))
        return plan

    def next_version(parent, event, mode=ReplanMode.FROM_EVENT):
        state = replan.prepare(parent, event, mode, travel)
        plan = service.build_plan(dataset, travel, Algorithm.BASELINE, state)
        assert plan.validation.ok, plan.validation.errors
        return stored(plan)

    morning = stored(service.build_plan(dataset, travel, Algorithm.BASELINE))
    gone = next_version(
        morning,
        EngineerUnavailableEvent(
            type="engineer_unavailable", time="09:10", engineer_id="brigade-1"
        ),
    )
    done = next_version(
        gone,
        CloseRequestEvent(
            type="close_request", time="10:30", request_id="1", outcome=Outcome.DONE
        ),
    )
    return dataset, morning, done, next_version


def rated(stars=5, **changes):
    return ReviewInput(stars=stars, **changes)


# --- оценка (70.1) -----------------------------------------------------------


def test_review_goes_to_who_did_the_work_not_to_the_morning_plan(day):
    _, morning, done, _ = day
    assert morning.assignments["1"] == "brigade-1"
    assert done.closed["1"].engineer_id == "brigade-2"

    review = reviews.save(done, "1", rated(4), ReviewSource.DISPATCHER)

    assert review.engineer_id == "brigade-2"
    assert db.reviews.find_one({"namespace": "test", "requestId": "1"})[
        "engineerId"
    ] == ("brigade-2")


def test_request_not_done_cannot_be_rated(day):
    _, _, done, next_version = day
    with pytest.raises(replan.ReplanError) as error:
        reviews.save(done, "2", rated(), ReviewSource.DISPATCHER)  # не закрыта
    assert error.value.status == 409
    assert "ещё не выполнена" in str(error.value)

    failed = next_version(
        done,
        CloseRequestEvent(
            type="close_request", time="11:00", request_id="2", outcome=Outcome.FAILED
        ),
    )
    with pytest.raises(replan.ReplanError, match="только выполненную"):
        reviews.save(failed, "2", rated(), ReviewSource.DISPATCHER)
    assert db.reviews.count_documents({}) == 0


def test_draft_cannot_be_rated(day):
    _, _, done, _ = day
    draft = done.model_copy(update={"draft": True})
    with pytest.raises(replan.ReplanError) as error:
        reviews.save(draft, "1", rated(), ReviewSource.DISPATCHER)
    assert error.value.status == 409


def test_dispatcher_edits_but_client_after_it_gets_409_without_overwrite(day):
    _, _, done, _ = day
    reviews.save(done, "1", rated(3, comment="первая"), ReviewSource.DISPATCHER)
    reviews.save(done, "1", rated(5, comment="правка"), ReviewSource.DISPATCHER)
    assert reviews.find(done, "1").comment == "правка"

    with pytest.raises(replan.ReplanError, match="уже оценена") as error:
        reviews.save(done, "1", rated(1, comment="клиент"), ReviewSource.CLIENT)

    assert error.value.status == 409
    kept = reviews.find(done, "1")
    assert (kept.stars, kept.comment, kept.source) == (
        5,
        "правка",
        ReviewSource.DISPATCHER,
    )
    assert db.reviews.count_documents({}) == 1


# --- ссылка клиента (70.3) ---------------------------------------------------


def test_token_round_trip_and_forgeries():
    token = reviews.token("abc", "74198")
    assert reviews.parse(token) == ("abc", "74198")

    body, signature = token.split(".")
    other = reviews.token("abc", "74199").split(".")[0]
    assert (
        reviews.parse(f"{other}.{signature}") is None
    )  # чужая заявка со старой подписью
    assert (
        reviews.parse(f"{body}.{'A' * len(signature)}") is None
    )  # подделанная подпись
    assert reviews.parse("мусор") is None
    assert reviews.parse("") is None


def test_client_link_opens_the_form_and_rates_once(day):
    _, _, done, _ = day
    client = TestClient(app)
    token = reviews.token(done.id, "1")

    form = client.get(f"/api/rate/{token}").json()
    assert form["engineerName"] == "Бригада 2"
    assert form["rated"] is False
    assert form["date"].startswith("2026-10-16")

    answer = client.post(f"/api/rate/{token}", json={"stars": 5, "tags": ["polite"]})
    assert answer.status_code == 200
    assert answer.json()["source"] == "client"
    assert client.get(f"/api/rate/{token}").json()["rated"] is True

    again = client.post(f"/api/rate/{token}", json={"stars": 1})
    assert again.status_code == 409
    assert again.json()["detail"] == "Эта заявка уже оценена"
    assert reviews.find(done, "1").stars == 5


def test_bad_links_answer_404(day):
    _, morning, done, _ = day
    client = TestClient(app)
    good = reviews.token(done.id, "1")
    forged = good[:-1] + ("A" if good[-1] != "A" else "B")

    for token in (forged, "мусор", reviews.token("нет-плана", "1")):
        assert client.get(f"/api/rate/{token}").status_code == 404
        assert client.post(f"/api/rate/{token}", json={"stars": 5}).status_code == 404
    # Подпись верная, но в этой версии заявка не выполнена.
    assert client.get(f"/api/rate/{reviews.token(morning.id, '1')}").status_code == 404


def test_dispatcher_endpoints(day, monkeypatch):
    _, _, done, _ = day
    monkeypatch.setattr(
        "app.config.settings.public_base_url", "http://192.168.1.5:3000/"
    )
    client = TestClient(app)
    url = f"/api/plans/{done.id}/requests/1/review"

    card = client.get(url).json()
    assert card["engineerName"] == "Бригада 2"
    assert card["review"] is None
    assert card["link"].startswith("http://192.168.1.5:3000/rate/")
    assert reviews.parse(card["link"].rsplit("/", 1)[1]) == (done.id, "1")

    saved = client.put(url, json={"stars": 4, "tags": ["fast"], "clientName": "Мария"})
    assert saved.status_code == 200
    assert client.get(url).json()["review"]["clientName"] == "Мария"
    assert client.put(url, json={"stars": 6}).status_code == 422
    assert client.put(url, json={"stars": 5, "tags": ["нет"]}).status_code == 422
    assert client.get(f"/api/plans/{done.id}/requests/2/review").status_code == 409
    assert client.get("/api/plans/нет/requests/1/review").status_code == 404


def test_plan_reviews_are_only_of_its_done_requests_and_its_people(day):
    # Чужое пространство людей с той же заявкой и невыполненная заявка 2 в ответ не попадают.
    _, _, done, _ = day
    reviews.save(done, "1", rated(4), ReviewSource.DISPATCHER)
    own = namespace(done.dataset_id)
    db.reviews.insert_one(
        {
            "namespace": "чужие",
            "requestId": "1",
            "engineerId": "x",
            "stars": 1,
            "tags": [],
            "comment": "",
            "clientName": "",
            "source": "demo",
            "at": datetime.now(UTC),
        }
    )
    db.reviews.insert_one(
        {
            "namespace": own,
            "requestId": "2",
            "engineerId": "x",
            "stars": 1,
            "tags": [],
            "comment": "",
            "clientName": "",
            "source": "demo",
            "at": datetime.now(UTC),
        }
    )

    answer = TestClient(app).get(f"/api/plans/{done.id}/reviews").json()

    assert [(item["requestId"], item["stars"]) for item in answer] == [("1", 4)]
    assert TestClient(app).get("/api/plans/нет/reviews").status_code == 404


# --- рейтинг (70.2) ----------------------------------------------------------


def put(engineer_id, request_id, stars, days_ago, now):
    review = Review(
        request_id=request_id,
        engineer_id=engineer_id,
        stars=stars,
        source=ReviewSource.DEMO,
        at=now - timedelta(days=days_ago),
    )
    db.reviews.insert_one({"namespace": "test"} | review.model_dump(by_alias=True))


def test_ratings_average_distribution_and_month_change(mongo):
    now = datetime(2026, 10, 16, tzinfo=UTC)
    put("a", "1", 3, 60, now)
    put("a", "2", 4, 40, now)
    put("a", "3", 5, 5, now)
    put("a", "4", 5, 1, now)
    put("b", "5", 5, 2, now)

    result = reviews.ratings("test", now)

    a = result["a"]
    assert (a.stars, a.count) == (4.25, 4)
    assert a.distribution == [2, 1, 1, 0, 0]
    assert a.month_change == 4.25 - 3.5  # месяц назад было две оценки: 3 и 4
    assert [review.request_id for review in a.last] == ["4", "3", "2", "1"]
    assert result["b"].month_change is None  # месяц назад отзывов не было
    assert reviews.ratings("другой", now) == {}


def test_start_rating_of_the_file_adds_up_with_the_reviews(mongo):
    """Стартовый рейтинг (блок 76) — оценки до этого дня: входят в среднюю и число и
    считаются старше месяца; распределение и последние — только по отзывам базы."""
    now = datetime(2026, 10, 16, tzinfo=UTC)
    put("a", "1", 5, 1, now)
    starts = {"a": Rating(stars=4.0, count=9), "b": Rating(stars=4.5, count=2)}

    result = reviews.ratings("test", now, starts)

    a = result["a"]
    assert (a.stars, a.count) == (4.1, 10)  # (4 × 9 + 5) / 10
    assert a.distribution == [1, 0, 0, 0, 0]
    assert a.month_change == pytest.approx(4.1 - 4.0)
    assert [review.request_id for review in a.last] == ["1"]
    # Без отзывов в базе — только рейтинг из файла.
    assert (result["b"].stars, result["b"].count, result["b"].month_change) == (
        4.5,
        2,
        0,
    )


def test_snapshot_takes_the_start_rating_with_the_reviews(day):
    dataset, _, done, _ = day
    start = Rating(stars=3.0, count=3)
    rated_dataset = dataset.model_copy(
        update={
            "engineers": [
                engineer.model_copy(update={"rating": start})
                for engineer in dataset.engineers
            ]
        }
    )
    reviews.save(done, "1", rated(5), ReviewSource.DISPATCHER)

    taken = {e.id: e.rating for e in reviews.snapshot(rated_dataset).engineers}

    assert taken == {
        "brigade-1": start,
        "brigade-2": Rating(stars=3.5, count=4),  # (3 × 3 + 5) / 4
    }


def test_ratings_endpoint_follows_a_new_review(day, monkeypatch):
    dataset, _, done, _ = day
    monkeypatch.setattr(seed, "current", lambda: dataset)
    client = TestClient(app)
    assert client.get("/api/ratings").json() == []

    reviews.save(done, "1", rated(4), ReviewSource.DISPATCHER)

    (rating,) = client.get("/api/ratings").json()
    assert (rating["engineerId"], rating["stars"], rating["count"]) == (
        "brigade-2",
        4,
        1,
    )
    (review,) = client.get("/api/engineers/brigade-2/reviews").json()
    assert review["requestId"] == "1"


# --- снимок (70.5) -----------------------------------------------------------


def test_snapshot_is_taken_at_calculation_and_stays_for_the_day(
    mongo, make_request, make_engineer, make_dataset, make_travel, monkeypatch
):
    requests = [
        make_request("1", windowEnd="18:00"),
        make_request("2", windowEnd="18:00"),
    ]
    dataset = make_dataset(
        requests, [make_engineer("brigade-1"), make_engineer("brigade-2")]
    )
    travel = make_travel(requests)
    monkeypatch.setattr(matrices, "get", lambda _: travel)
    monkeypatch.setattr(valhalla, "route", lambda locations, costing: [list(locations)])
    put("brigade-1", "900", 4, 3, datetime.now(UTC))

    morning = service.create_plan(dataset)

    by_id = {engineer.id: engineer for engineer in morning.input.engineers}
    assert by_id["brigade-1"].rating.model_dump() == {"stars": 4.0, "count": 1}
    assert by_id["brigade-2"].rating is None
    assert dataset.engineers[0].rating is None  # набор не тронут

    # Новый отзыв днём снимок следующей версии не меняет.
    put("brigade-1", "901", 1, 0, datetime.now(UTC))
    closed = service.replan_plan(
        morning,
        dataset,
        CloseRequestEvent(
            type="close_request",
            time="10:30",
            request_id=morning.routes[0].stops[0].request_id,
            outcome=Outcome.DONE,
        ),
        ReplanMode.FROM_EVENT,
    )
    assert closed.input.engineers == morning.input.engineers


def test_new_review_does_not_reset_the_solution_cache(
    mongo, make_request, make_engineer, make_dataset, make_travel
):
    requests = [make_request("1")]
    dataset = make_dataset(requests, [make_engineer()])
    travel = make_travel(requests)
    before = service._fingerprint(reviews.snapshot(dataset), travel, None)
    put("brigade-1", "900", 5, 3, datetime.now(UTC))
    after = reviews.snapshot(dataset)

    assert after.engineers[0].rating is not None
    assert service._fingerprint(after, travel, None) == before


def test_new_review_resets_the_cache_of_rating_first(
    mongo, make_request, make_engineer, make_dataset, make_travel
):
    """В «Рейтинге в приоритете» рейтинг решает, кому работа (блок 71): он в отпечатке."""
    requests = [make_request("1")]
    dataset = make_dataset(requests, [make_engineer()])
    travel = make_travel(requests)
    setup = variants.SETUPS[Variant.RATING_FIRST]
    before = service._fingerprint(reviews.snapshot(dataset), travel, setup)
    put("brigade-1", "900", 5, 3, datetime.now(UTC))

    assert service._fingerprint(reviews.snapshot(dataset), travel, setup) != before


# --- демо-история -------------------------------------------------------------


def test_seeded_history_is_idempotent_and_dated_from_now(mongo, tmp_path, monkeypatch):
    folder = tmp_path / "seed"
    folder.mkdir()
    history = [
        {
            "requestId": "600000",
            "engineerId": "brigade-1",
            "stars": 5,
            "tags": ["fast"],
            "comment": "",
            "clientName": "",
            "daysAgo": 10,
        }
    ]
    (folder / "reviews.json").write_text(json.dumps(history), "utf-8")
    monkeypatch.setattr(seed.settings, "data_dir", tmp_path)
    now = datetime(2026, 10, 16, tzinfo=UTC)

    seed.seed_reviews(now)
    seed.seed_reviews(now)

    (stored,) = mongo.reviews.find()
    assert stored["namespace"] == "demo"
    assert stored["at"].replace(tzinfo=UTC) == now - timedelta(days=10)


def test_generated_history_repeats_and_leaves_novices_empty(tmp_path, monkeypatch):
    from scripts import prepare_data

    out = tmp_path / "reviews.json"
    monkeypatch.setattr(prepare_data, "REVIEWS_PATH", out)
    monkeypatch.setattr(prepare_data, "DATA", tmp_path)

    prepare_data.write_reviews(1)
    first = out.read_text("utf-8")
    prepare_data.write_reviews(1)

    assert out.read_text("utf-8") == first
    history = json.loads(first)
    pool = json.loads(prepare_data.CREWS_PATH.read_text("utf-8"))
    rated_people = {item["engineerId"] for item in history}
    # Отзывы есть у всех (блок 71): в пространстве демо-людей новичков нет.
    assert rated_people == {engineer["id"] for engineer in pool}
    assert all(len(item["requestId"]) == 6 for item in history)
    assert len({item["requestId"] for item in history}) == len(history)
    # Файл в репозитории собран этим же seed.
    assert json.loads(
        (seed.settings.data_dir / "seed" / "reviews.json").read_text("utf-8")
    ) == (history)


def test_engineer_added_during_the_day_brings_no_rating(day, make_engineer):
    """Рейтинг ставит расчёт, а не тело события: подставленный в событие — не снимок."""
    from app.models import AddEngineerEvent

    _, _, done, next_version = day
    joined = next_version(
        done,
        AddEngineerEvent(
            type="add_engineer",
            time="11:00",
            engineer=make_engineer("brigade-9", rating={"stars": 5, "count": 99}),
        ),
    )
    added = next(item for item in joined.input.engineers if item.id == "brigade-9")
    assert added.rating is None

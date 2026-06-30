from src.integrations.workua_api import parse_response, parse_resume


def test_parse_response_minimal():
    raw = {
        "id": "1234",
        "job_id": "55",
        "candidate_id": "9876",
        "date": "2026-06-22T10:30:00+03:00",
        "fio": "Іван Петренко",
        "from_type": "send",
        "birth_date": "1990-05-15",
        "email": "ivan@example.com",
        "phone": "+380671234567",
        "type": "resume",
        "with_file": "0",
        "text": "досвід менеджера з продажу 3 роки",
        "cover": None,
        "preferCommunicationChannels": ["phone"],
    }
    r = parse_response(raw)
    assert r.id == 1234
    assert r.job_id == 55
    assert r.fio == "Іван Петренко"
    assert r.phone == "+380671234567"
    assert r.from_type == "send"
    assert r.with_file is False
    assert r.prefer_channels == ["phone"]


def test_parse_response_phonecall_type():
    raw = {
        "id": "5",
        "candidate_id": "1",
        "from_type": "phonecall",
        "with_file": "1",
        "type": "easy",
    }
    r = parse_response(raw)
    assert r.from_type == "phonecall"
    assert r.with_file is True


def test_parse_resume_with_contacts():
    raw = {
        "result": {
            "resume_id": 999,
            "first_name": "Олена",
            "last_name": "Петрівна",
            "name": "Менеджер з продажу",
            "region": "Львів",
            "birth_date": "1995-03-10",
            "salary": "30000",
            "sex_rid": "86",
            "contacts": {
                "phone": "+380501234567",
                "email": "olena@example.com",
                "exprns": [
                    {
                        "position": "Sales manager",
                        "company": "ТОВ Альфа",
                        "start_date": "2022-01-01",
                        "end_date": "2026-06-01",
                    }
                ],
            },
        }
    }
    r = parse_resume(raw)
    assert r.resume_id == 999
    assert r.first_name == "Олена"
    assert r.phone == "+380501234567"
    assert r.salary == 30000
    assert len(r.experiences) == 1
    assert r.experiences[0]["position"] == "Sales manager"

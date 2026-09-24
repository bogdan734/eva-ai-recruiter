"""LD_1001 «Вакансія» (src/common/vacancies.py Vacancy.label) is a KeyCRM
SELECT field, not free text -- confirmed live 07.09.2026 via GET
/custom-fields (type=select) and by probing directly: POSTing/PUTting a value
that isn't one of KeyCRM's predefined options is silently dropped (the card
saves fine, but the field renders empty, "+ Додати") with no error anywhere in
our code or KeyCRM's response to catch.

Incident: LOGISTICS_JR shipped with its own descriptive title
("Менеджер з логістики та продажу транспортних послуг (Junior)") as `label`,
which looks correct but isn't one of KeyCRM's options -- 6 of the vacancy's 8
live cards had a blank «Вакансія» field as a result. LOGISTICS_JR shares
keycrm_pipeline_id=1 with SALES ("1 Етап Менеджер з продажу"), and that
pipeline's only confirmed-valid option is SALES.label -- so every vacancy
routed into pipeline 1 must reuse that exact string; the specific posting
still comes through via LD_1002 (vacancy_number) and LD_1004 (vacancy_url),
which are free-text/link fields and were never affected.

This does not (and cannot, without a live KeyCRM call) validate against
KeyCRM's actual option list -- it pins the one failure mode we already hit.
"""
from src.common import vacancies


def test_every_vacancy_sharing_the_sales_pipeline_reuses_its_label():
    sales = vacancies.SALES
    for v in vacancies.SHIPPED.values():
        if v.keycrm_pipeline_id == sales.keycrm_pipeline_id:
            assert v.label == sales.label, (
                f"{v.key!r} shares pipeline {sales.keycrm_pipeline_id} with "
                f"'sales' but has a different label ({v.label!r}) -- KeyCRM's "
                f"LD_1001 select will silently blank the card unless this "
                f"matches an option that actually exists, and 'sales' is the "
                f"only value confirmed live in this pipeline."
            )


def test_logistics_jr_specifically_uses_the_confirmed_valid_option():
    """Named regression for the exact incident, not just the general rule
    above -- if this ever fails while the general rule passes, the general
    rule itself needs revisiting, not just this vacancy."""
    assert vacancies.LOGISTICS_JR.label == "Менеджер з продажу"


def test_accountant_is_its_own_pipeline_and_is_unaffected():
    """Sanity check the general rule isn't accidentally over-broad: ACCOUNTANT
    lives in a different pipeline (6) and correctly keeps its own label."""
    assert vacancies.ACCOUNTANT.keycrm_pipeline_id != vacancies.SALES.keycrm_pipeline_id
    assert vacancies.ACCOUNTANT.label == "Бухгалтер"

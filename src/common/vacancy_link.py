"""Which posting a card should point at, on the board the person answered.

Every robota.ua card reached the recruiter with «Номер вакансії» and
«Посилання на вакансію» empty. Nothing filled them: LD_1002 only ever received a
work.ua response id, and LD_1004 received the applicant's résumé link — useful,
but not what a field named «Посилання на вакансію» promises.

The board id has to travel with the applicant. A vacancy carries several ids per
board (a posting republished under a new number is still the same job), so the
right link cannot be derived from the vacancy alone — only from the record the
person's application arrived in.
"""
from __future__ import annotations

# work.ua exposes postings publicly; robota.ua answers 403 to anything that is
# not a signed-in browser, so the useful destination there is the employer
# cabinet — which is where the recruiter reading the card already is.
_BOARD_URLS = {
    "workua": "https://www.work.ua/jobs/{id}/",
    "robotaua": "https://robota.ua/my/vacancies/{id}/candidates",
}


def vacancy_number_and_url(
    source: str | None, board_vacancy_id: int | None, route
) -> tuple[str, str]:
    """(«Номер вакансії», «Посилання на вакансію») for a card being created.

    Falls back to whatever the vacancy itself carries when the board cannot be
    read off the source, or when no id came with the applicant. Inventing a link
    from an id we cannot attribute would point the recruiter at the wrong board.
    """
    if board_vacancy_id:
        for token in (source or "").split(","):
            prefix = token.strip().lower().split("_")[0]
            template = _BOARD_URLS.get(prefix)
            if template:
                return str(board_vacancy_id), template.format(id=board_vacancy_id)
    return (
        str(getattr(route, "vacancy_number", "") or ""),
        str(getattr(route, "vacancy_url", "") or ""),
    )

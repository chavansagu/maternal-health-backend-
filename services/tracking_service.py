from datetime import date, datetime


def get_tracking_info(pw) -> dict:
    """
    Gestational age is calculated from LMP up to today.
    28+ weeks and still active -> 'enhanced', otherwise 'regular'.
    """
    weeks = None
    if pw is not None and pw.lmp_date:
        lmp = pw.lmp_date.date() if isinstance(pw.lmp_date, datetime) else pw.lmp_date
        weeks = (date.today() - lmp).days // 7

    is_delivered = pw is not None and (
        not pw.is_active or pw.pregnancy_outcome is not None
    )

    tracking_type = (
        "enhanced"
        if (weeks is not None and weeks >= 28 and not is_delivered)
        else "regular"
    )

    return {
        "gestational_weeks": weeks,
        "tracking_type": tracking_type,
    }
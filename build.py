"""Считает аналитику клуба «Территория Нити Жизни» из raw/deals.json → site/data.json (без личных данных)."""
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
GRACE = timedelta(days=7)  # оплата в течение недели после окончания доступа считается продлением
GROUPS = ["4897279"]  # «Актив.участник ТНЖ»

# ---------- классификация заказов ----------

CLUB_RE = re.compile(r"Территория Нит[ьи] Жизни")


def classify(title):
    if CLUB_RE.search(title):
        return "club"
    return None


def months_of(title):
    m = re.search(r"(\d+)\s*месяц", title)
    return int(m.group(1)) if m else 1


def days_of(months):
    return {1: 30, 3: 90, 6: 180, 12: 365}.get(months, months * 30)


def tier_of(title):
    return "ВИП" if "ВИП" in title else "Стандарт"


def is_auto(title):
    return "автопродление" in title


SOURCE_GROUPS = [
    ("Рассылки", r"^mail|e\.mail|email|unisender|sendpulse"),
    ("Telegram", r"^tg|telegram|t\.me"),
    ("Instagram", r"insta|^ig"),
    ("ВКонтакте", r"^vk|vkontakte"),
    ("YouTube", r"youtube|^yt"),
    ("Вебинары", r"bizon|webinar|вебинар"),
    ("Поиск", r"google|yandex|ya\.ru"),
    ("Сайты Анны и Владимира", r"annaosipova|vladimirosipov"),
    ("Прямые заходы", r"\(direct\)"),
]


def source_group(src):
    if not src:
        return "Без метки"
    for name, rx in SOURCE_GROUPS:
        if re.search(rx, src, re.I):
            return name
    return "Другие"


# ---------- загрузка ----------

def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


def load(now):
    with open(os.path.join(HERE, "raw", "deals.json"), encoding="utf-8") as f:
        raw = json.load(f)
    ix = {k: i for i, k in enumerate(raw["fields"])}
    deals = []
    for r in raw["items"]:
        title = r[ix["Состав заказа"]]
        kind = classify(title)
        uid = r[ix["ID пользователя"]]
        if not kind or not uid:
            continue
        paid = r[ix["Оплачен"]] == "Да"
        created = parse_dt(r[ix["Дата создания"]])
        pay_dt = parse_dt(r[ix["Дата оплаты"]]) or (created if paid else None)
        if pay_dt and pay_dt > now:
            continue
        deals.append({
            "uid": uid,
            "kind": kind,
            "title": title,
            "paid": paid,
            "status": r[ix["Статус"]],
            "created": created,
            "pay_dt": pay_dt,
            "amount": float(r[ix["Оплачено"]] or 0),
            "manager": re.sub(r"^(\S+) \1 ", r"\1 ", r[ix["Менеджер"]] or ""),
            "source": next((r[ix[k]] for k in ("user_utm_source", "utm_source", "deal_utm_source", "gc_system_deal_utm_source") if k in ix and r[ix[k]]), ""),
        })
    return deals


# ---------- периоды доступа ----------

def build_members(deals):
    """Для каждого участника — список оплат клуба и непрерывных блоков доступа."""
    members = defaultdict(lambda: {"payments": [], "trials": [], "source": ""})
    for d in deals:
        m = members[d["uid"]]
        if d["kind"] == "trial":
            if d["paid"] or (d["amount"] == 0 and d["status"] != "Отменен"):
                m["trials"].append(d["created"])
            continue
        if not d["paid"] or d["kind"] == "free":
            continue
        months = 3 if d["kind"] == "trip" else months_of(d["title"])
        m["payments"].append({
            "dt": d["pay_dt"],
            "amount": 0.0 if d["kind"] == "trip" else d["amount"],
            "months": months,
            "days": 90 if d["kind"] == "trip" else days_of(months),
            "tier": tier_of(d["title"]),
            "auto": is_auto(d["title"]) or d["title"].startswith("Продление Покупка"),
            "manager": d["manager"],
            "trip": d["kind"] == "trip",
            "source": d["source"],
        })
    out = {}
    for uid, m in members.items():
        pays = sorted(m["payments"], key=lambda p: p["dt"])
        blocks = []
        for p in pays:
            if blocks and p["dt"] <= blocks[-1]["end"] + GRACE:
                b = blocks[-1]
                p["type"] = "renew"
                start = max(p["dt"], b["end"])
                b["end"] = start + timedelta(days=p["days"])
                b["pays"].append(p)
            else:
                p["type"] = "new" if not blocks else "return"
                blocks.append({"start": p["dt"], "end": p["dt"] + timedelta(days=p["days"]), "pays": [p]})
            p["period_end"] = blocks[-1]["end"]
        first_src = next((p["source"] for p in pays if p["source"]), "")
        out[uid] = {"payments": pays, "blocks": blocks, "trials": sorted(t for t in m["trials"] if t),
                    "source": first_src}
    return out


def active_at(mem, dt):
    return any(b["start"] <= dt < b["end"] for b in mem["blocks"])


def month_key(dt):
    return dt.strftime("%Y-%m")


def month_end(key):
    y, m = map(int, key.split("-"))
    return datetime(y + (m == 12), m % 12 + 1, 1)


def add_months(key, n):
    y, m = map(int, key.split("-"))
    m += n
    return f"{y + (m - 1) // 12}-{(m - 1) % 12 + 1:02d}"


MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
             "сентябрь", "октябрь", "ноябрь", "декабрь"]


def month_name(key):
    y, m = map(int, key.split("-"))
    return f"{MONTHS_RU[m - 1]} {y}"


# ---------- расчёт ----------

def compute(now):
    deals = load(now)
    mem = build_members(deals)
    paying = {u: m for u, m in mem.items() if m["payments"]}
    all_pays = [p for m in paying.values() for p in m["payments"]]
    first_month = min(month_key(p["dt"]) for p in all_pays)
    cur = month_key(now)
    months = []
    k = first_month
    while k <= cur:
        months.append(k)
        k = add_months(k, 1)

    active_now = {u for u, m in paying.items() if active_at(m, now)}

    def mrr_at(dt):
        total = 0.0
        for m in paying.values():
            for b in m["blocks"]:
                if b["start"] <= dt < b["end"]:
                    p = max((p for p in b["pays"] if p["dt"] <= dt), key=lambda p: p["dt"], default=None)
                    if p and p["amount"]:
                        total += p["amount"] / p["months"]
        return total

    # по месяцам
    monthly = []
    for k in months:
        start = datetime.strptime(k + "-01", "%Y-%m-%d")
        end = min(month_end(k), now)
        pays = [p for p in all_pays if start <= p["dt"] < end]
        new = sum(1 for p in pays if p["type"] == "new")
        ret = sum(1 for p in pays if p["type"] == "return")
        # продления: периоды, закончившиеся в этом месяце и уже «созревшие»
        due = renewed = churned = 0
        for m in paying.values():
            for b in m["blocks"]:
                for i, p in enumerate(b["pays"]):
                    pend = p["period_end"]
                    if start <= pend < end and pend + GRACE <= now:
                        due += 1
                        renewed += i + 1 < len(b["pays"])
                if start <= b["end"] < end and b["end"] + GRACE <= now:
                    churned += 1
        monthly.append({
            "month": k,
            "name": month_name(k),
            "new": new,
            "returned": ret,
            "churned": churned,
            "net": new + ret - churned,
            "active_end": sum(1 for m in paying.values() if active_at(m, end - timedelta(seconds=1))),
            "rev_new": round(sum(p["amount"] for p in pays if p["type"] == "new")),
            "rev_return": round(sum(p["amount"] for p in pays if p["type"] == "return")),
            "rev_renew": round(sum(p["amount"] for p in pays if p["type"] == "renew")),
            "mrr": round(mrr_at(end - timedelta(seconds=1))),
            "due": due,
            "renewed": renewed,
            "renewal_rate": round(renewed / due * 100, 1) if due else None,
            "partial": k == cur,
        })

    # тарифы: этот и прошлый месяц
    def plan_table(k):
        start = datetime.strptime(k + "-01", "%Y-%m-%d")
        end = min(month_end(k), now)
        rows = defaultdict(lambda: [0, 0.0])
        for p in all_pays:
            if start <= p["dt"] < end and not p["trip"]:
                kind = "Новые" if p["type"] in ("new", "return") else "Продление"
                key = f"{kind} · {p['months']} мес · {'автосписание' if p['auto'] else 'по ссылке'}"
                rows[key][0] += 1
                rows[key][1] += p["amount"]
        return {k2: [v[0], round(v[1])] for k2, v in rows.items()}

    prev = add_months(cur, -1)
    plans_cur, plans_prev = plan_table(cur), plan_table(prev)
    plan_keys = sorted(set(plans_cur) | set(plans_prev),
                       key=lambda s: (s.split(" · ")[0] != "Новые", s))
    plans = [{"plan": key, "cur": plans_cur.get(key, [0, 0]), "prev": plans_prev.get(key, [0, 0])}
             for key in plan_keys]

    # окончания доступа у действующих
    def current_block(m):
        return next(b for b in m["blocks"] if b["start"] <= now < b["end"])

    ends = []
    for u in active_now:
        m = paying[u]
        b = current_block(m)
        ends.append((b["end"], b["pays"][-1]["auto"]))
    exp_days = []
    for n in (1, 3, 7, 14):
        lim = now + timedelta(days=n)
        sel = [e for e in ends if e[0] <= lim]
        exp_days.append({"days": n, "count": len(sel), "manual": sum(1 for e in sel if not e[1])})
    exp_months = []
    for i in range(4):
        k = add_months(cur, i)
        sel = [e for e in ends if month_key(e[0]) == k]
        exp_months.append({"month": k, "name": month_name(k), "count": len(sel),
                           "manual": sum(1 for e in sel if not e[1])})
    autopay = sum(1 for e in ends if e[1])

    # LTV и срок жизни
    ltv, life = [], []
    for m in paying.values():
        ltv.append(sum(p["amount"] for p in m["payments"]))
        life.append(sum((min(b["end"], now) - b["start"]).days for b in m["blocks"]) / 30.44)
    mature = [m for m in paying.values() if m["payments"][0]["dt"] <= now - timedelta(days=183)]
    share_6m = (sum(1 for m in mature
                    if sum((min(b["end"], now) - b["start"]).days for b in m["blocks"]) >= 180)
                / len(mature) * 100) if mature else None

    # давность у тех, кто сейчас в клубе
    buckets = [("до 1 месяца", 0, 1), ("1–3 месяца", 1, 3), ("3–6 месяцев", 3, 6),
               ("6–12 месяцев", 6, 12), ("больше года", 12, 999)]
    ages = [(now - paying[u]["payments"][0]["dt"]).days / 30.44 for u in active_now]
    tenure = [{"label": lbl, "count": sum(1 for a in ages if lo <= a < hi)} for lbl, lo, hi in buckets]
    for t in tenure:
        t["share"] = round(t["count"] / len(ages) * 100, 1) if ages else 0

    # источники
    src = defaultdict(list)
    for m in paying.values():
        src[source_group(m["source"])].append(m)
    sources = []
    for name, ms in src.items():
        sources.append({
            "source": name,
            "users": len(ms),
            "active": sum(1 for m in ms if active_at(m, now)),
            "ltv": round(statistics.mean(sum(p["amount"] for p in m["payments"]) for m in ms)),
            "months": round(statistics.mean(
                sum((min(b["end"], now) - b["start"]).days for b in m["blocks"]) / 30.44 for m in ms), 1),
            "renewed": round(sum(1 for m in ms if len(m["payments"]) > 1) / len(ms) * 100),
        })
    sources.sort(key=lambda s: -s["users"])

    # когорты
    cohorts = []
    for k in months[-13:-1]:
        cohort = [m for m in paying.values() if month_key(m["payments"][0]["dt"]) == k]
        if not cohort:
            continue
        ret = []
        n = 1
        while add_months(k, n) < cur:
            dt = month_end(add_months(k, n)) - timedelta(seconds=1)
            ret.append(round(sum(1 for m in cohort if active_at(m, dt)) / len(cohort) * 100))
            n += 1
        cohorts.append({"month": k, "name": month_name(k), "size": len(cohort), "ret": ret[:11]})

    # пробный доступ
    def trial_stats(since):
        started = conv = 0
        for m in mem.values():
            ts = [t for t in m["trials"] if t >= since]
            if not ts:
                continue
            started += 1
            t0 = ts[0]
            if any(t0 <= p["dt"] <= t0 + timedelta(days=30) for p in m["payments"]):
                conv += 1
        return {"started": started, "paid": conv, "conv": round(conv / started * 100, 1) if started else None}

    trial = {"m3": trial_stats(now - timedelta(days=91)), "all": trial_stats(datetime(2000, 1, 1))}

    # база и архив
    ever = set(paying)
    renewed_once = {u for u, m in paying.items() if len(m["payments"]) > 1}
    trial_users = {u for u, m in mem.items() if m["trials"]}
    archive = {
        "total": len((ever | trial_users) - active_now),
        "trial_no_buy": len(trial_users - ever),
        "bought_once": len({u for u in ever - active_now if len(paying[u]["payments"]) == 1}),
        "renewed_left": len({u for u in ever - active_now if len(paying[u]["payments"]) > 1}),
    }

    # менеджеры
    def managers(k):
        start = datetime.strptime(k + "-01", "%Y-%m-%d")
        end = min(month_end(k), now)
        rows = defaultdict(lambda: {"manual": [0, 0.0], "auto": [0, 0.0]})
        for p in all_pays:
            if start <= p["dt"] < end and p["manager"]:
                r = rows[p["manager"]]["auto" if p["auto"] else "manual"]
                r[0] += 1
                r[1] += p["amount"]
        return sorted(({"name": n, "manual": [v["manual"][0], round(v["manual"][1])],
                        "auto": [v["auto"][0], round(v["auto"][1])]} for n, v in rows.items()),
                      key=lambda r: -(r["manual"][1] + r["auto"][1]))

    # участники групп клуба (доступ в GetCourse) — если выгрузка групп есть
    group_ids = set()
    for g in GROUPS:
        path = os.path.join(HERE, "raw", f"group_{g}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                gr = json.load(f)
            gix = gr["fields"].index("id")
            group_ids |= {str(r[gix]) for r in gr["items"]}
    in_group = {
        "total": len(group_ids),
        "paid": len(group_ids & active_now),
        "no_payments": len(group_ids - set(paying)),
        "lapsed": len(group_ids & (set(paying) - active_now)),
    } if group_ids else None

    cm = monthly[-1]
    pm = monthly[-2] if len(monthly) > 1 else cm
    data = {
        "updated": now.strftime("%d.%m, %H:%M"),
        "kpi": {
            "active": len(active_now),
            "ever": len(ever),
            "rev_month": cm["rev_new"] + cm["rev_return"] + cm["rev_renew"],
            "rev_prev": pm["rev_new"] + pm["rev_return"] + pm["rev_renew"],
            "new_month": cm["new"] + cm["returned"],
            "new_month_rub": cm["rev_new"] + cm["rev_return"],
            "renew_month": sum(1 for p in all_pays if p["type"] == "renew" and month_key(p["dt"]) == cur),
            "renew_month_rub": cm["rev_renew"],
            "autopay_share": round(autopay / len(active_now) * 100) if active_now else 0,
            "autopay": autopay,
            "expiring_month": exp_months[0]["count"],
            "day_of_month": now.day,
        },
        "in_group": in_group,
        "monthly": monthly,
        "plans": plans,
        "expiring": {"days": exp_days, "months": exp_months},
        "base": {"ever": len(ever), "renewed": len(renewed_once), "active": len(active_now)},
        "archive": archive,
        "ltv": {
            "avg": round(statistics.mean(ltv)), "median": round(statistics.median(ltv)),
            "avg_months": round(statistics.mean(life), 1), "median_months": round(statistics.median(life), 1),
            "share_6m": round(share_6m, 1) if share_6m is not None else None,
        },
        "tenure": tenure,
        "sources": sources,
        "cohorts": cohorts,
        "trial": trial,
        "managers": {"cur": managers(cur), "prev": managers(prev),
                     "cur_name": month_name(cur), "prev_name": month_name(prev)},
    }
    data["insights"] = insights(data)
    return data


def fmt(n):
    return f"{round(n):,}".replace(",", " ")


def insights(data):
    full = [m for m in data["monthly"] if not m["partial"]]
    if len(full) < 2:
        return {"month": "", "items": []}
    m, p = full[-1], full[-2]
    year = [x["renewal_rate"] for x in full[-12:] if x["renewal_rate"] is not None]
    out = []

    def add(good, title, text):
        out.append({"tone": "good" if good else "bad", "title": title, "text": text})

    if p["mrr"]:
        ch = (m["mrr"] - p["mrr"]) / p["mrr"] * 100
        add(ch >= 0, f"Выручка в месяц от подписок {'выросла' if ch >= 0 else 'снизилась'} на {abs(ch):.0f}%",
            f"{fmt(m['mrr'])} ₽ против {fmt(p['mrr'])} ₽ месяцем раньше. Это «база» клуба без разовых всплесков.")
    if m["renewal_rate"] is not None:
        avg = statistics.mean(year) if year else m["renewal_rate"]
        add(m["renewal_rate"] >= avg,
            f"Продлили {m['renewal_rate']:.0f}% участников",
            f"Из {m['due']} человек, у кого закончился оплаченный период, продлили {m['renewal_rate']:.0f}% "
            f"(месяцем раньше {p['renewal_rate'] or 0:.0f}%, в среднем за год {avg:.0f}%). "
            "Продление — управляемый процесс: касания перед окончанием доступа и выгодные условия постоянным.")
    net = m["net"]
    add(net >= 0, f"Клуб {'вырос' if net >= 0 else 'сократился'} на {abs(net)} чел.",
        f"Пришли {m['new']} новых и вернулись {m['returned']}, ушли {m['churned']}. "
        f"На конец месяца в клубе {m['active_end']} (месяцем раньше {p['active_end']})."
        + ("" if net >= 0 else " Новых меньше, чем ушедших — клубу нужен приток: прогрев внутри основных продуктов и работа с архивом."))
    lt = data["ltv"]
    if lt["share_6m"] is not None:
        add(lt["avg_months"] >= 6, f"Участник в среднем остаётся {lt['avg_months']:.1f} мес".replace(".", ","),
            f"Ориентир — от 6 месяцев. До полугода доживают {lt['share_6m'] or 0:.0f}% участников, средний LTV {fmt(lt['avg'])} ₽.")
    if p["new"]:
        ch = (m["new"] - p["new"]) / p["new"] * 100
        add(ch >= 0, f"Новых участников {'больше' if ch >= 0 else 'меньше'} на {abs(ch):.0f}%",
            f"{m['new']} против {p['new']}. Лучшее время продать клуб — сразу после окончания основного продукта, "
            "пока у выпускников максимум энтузиазма.")
    t = data["trial"]["m3"]
    if t["started"] >= 10:
        add(t["conv"] >= 15, f"Пробный доступ → оплата: {t['conv']:.0f}%".replace(".", ","),
            f"За 3 месяца пробный доступ взяли {t['started']} чел., оплатили клуб в течение 30 дней — {t['paid']}.")
    return {"month": m["name"], "items": out}


def encrypt(data, password):
    """AES-GCM, ключ из пароля через PBKDF2-SHA256 — расшифровывается в браузере (index.html)."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv, iterations = os.urandom(16), os.urandom(12), 250_000
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(
        password.encode("utf-8"))
    blob = AESGCM(key).encrypt(iv, json.dumps(data, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "iter": iterations, "salt": b64(salt), "iv": b64(iv), "data": b64(blob)}


if __name__ == "__main__":
    from zoneinfo import ZoneInfo

    # время GetCourse — московское, на GitHub часы в UTC
    now = datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    data = compute(now)
    site = os.path.join(HERE, "site")
    os.makedirs(site, exist_ok=True)
    password = os.environ.get("SITE_PASSWORD")
    if password:
        with open(os.path.join(site, "data.enc.json"), "w", encoding="utf-8") as f:
            json.dump(encrypt(data, password), f)
        print("Зашифрованные данные записаны в site/data.enc.json")
    else:
        with open(os.path.join(site, "data.plain.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print("SITE_PASSWORD не задан — записан незашифрованный site/data.plain.json (не публиковать)")
    print("ok", data["kpi"])


import re
import json
import sqlite3
import hashlib
from datetime import datetime
from urllib.parse import quote_plus

import feedparser
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

APP_NAME = "Galatasaray Transfer Radar Merkezi Plus"
DB_FILE = "transfer_radar.db"
CONFIG_FILE = "sources.json"
WATCHLIST_FILE = "watchlist.json"

TRANSFER_KEYWORDS = [
    "galatasaray", "transfer", "anlaşma", "imza", "teklif", "bonservis", "kiralık",
    "görüşme", "menajer", "kap", "resmi", "sağlık kontrolü", "sarı kırmızılı",
    "interest", "offer", "deal", "sign", "signed", "loan", "bid", "talks",
    "medical", "agreement", "official", "confirmed", "clause", "here we go",
    "negotiation", "verbal agreement", "personal terms"
]

LOW_CONFIDENCE_WORDS = [
    "iddia", "söylenti", "gündem", "radar", "liste", "yazdı", "önerildi",
    "claim", "rumour", "rumor", "linked", "monitoring", "could", "may", "considering"
]

HIGH_CONFIDENCE_WORDS = [
    "kap", "resmi", "açıklandı", "duyurdu", "imzaladı", "sağlık kontrolü",
    "official", "confirmed", "announced", "medical", "signed", "agreement reached",
    "here we go", "personal terms agreed", "verbal agreement"
]

EXPERT_NAMES = [
    "fabrizio romano", "gianluca di marzio", "matteo moretto",
    "david ornstein", "santi aouna", "nicolo schira"
]

POSITION_WORDS = {
    "Kaleci": ["kaleci", "goalkeeper"],
    "Stoper": ["stoper", "defender", "centre-back", "center-back"],
    "Bek": ["sağ bek", "sol bek", "right-back", "left-back", "full-back"],
    "Orta Saha": ["orta saha", "midfielder", "midfield"],
    "Kanat": ["kanat", "winger", "wing"],
    "Forvet": ["forvet", "striker", "forward", "santrfor"]
}

PLAYER_REGEX = re.compile(
    r"\b([A-ZÇĞİÖŞÜ][a-zçğıöşü'’.-]+(?:\s+[A-ZÇĞİÖŞÜ][a-zçğıöşü'’.-]+){1,3})\b"
)

BANNED_NAMES = {
    "Galatasaray", "Sarı Kırmızılı", "Sarı Kırmızılılar", "Transfer Haberleri",
    "Son Dakika", "Süper Lig", "Şampiyonlar Ligi", "UEFA Avrupa",
    "Galatasaray Transfer", "Aslan Transfer", "Cimbom Transfer", "Galatasaray Haberleri"
}

st.set_page_config(page_title=APP_NAME, page_icon="🟡", layout="wide")

st.markdown("""
<style>
.main .block-container {padding-top: 1.2rem;}
.news-card {
    border: 1px solid rgba(255,204,0,.28);
    border-radius: 16px;
    padding: 18px;
    background: #11111108;
    margin-bottom: 12px;
}
.alert-card {
    border: 2px solid rgba(198,40,40,.55);
    border-radius: 16px;
    padding: 18px;
    background: rgba(198,40,40,.08);
    margin-bottom: 12px;
}
.score-high {color:#13a10e;font-weight:900;}
.score-mid {color:#b8860b;font-weight:900;}
.score-low {color:#c62828;font-weight:900;}
.small {font-size:13px;opacity:.8;}
.badge {
    display:inline-block;
    padding:4px 8px;
    border-radius:999px;
    background:#ffcc00;
    color:#8b0000;
    font-weight:800;
    font-size:12px;
}
</style>
""", unsafe_allow_html=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id TEXT PRIMARY KEY,
            title TEXT,
            link TEXT,
            source TEXT,
            source_type TEXT,
            published TEXT,
            summary TEXT,
            score INTEGER,
            confidence TEXT,
            players TEXT,
            positions TEXT,
            alarm TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    con.commit()
    con.close()


def clean_html(text):
    return re.sub("<.*?>", "", text or "").replace("&nbsp;", " ").strip()


def make_id(title, link):
    return hashlib.md5(f"{title}|{link}".encode("utf-8")).hexdigest()


def fetch_feed(source):
    feed = feedparser.parse(source["url"])
    rows = []
    for e in feed.entries[: source.get("limit", 25)]:
        title = clean_html(getattr(e, "title", ""))
        link = getattr(e, "link", "")
        summary = clean_html(getattr(e, "summary", ""))
        published = getattr(e, "published", "") or getattr(e, "updated", "")

        if not title:
            continue

        full_text = f"{title} {summary}"
        rows.append({
            "id": make_id(title, link),
            "source": source["name"],
            "source_type": source.get("type", "news"),
            "weight": source.get("weight", 50),
            "title": title,
            "summary": summary,
            "link": link,
            "published": published,
            "raw_text": full_text.lower(),
            "display_text": full_text
        })
    return rows


def extract_players(text):
    candidates = PLAYER_REGEX.findall(text)
    cleaned = []
    for c in candidates:
        c = c.strip()
        if c in BANNED_NAMES:
            continue
        if "Galatasaray" in c and len(c.split()) <= 2:
            continue
        if len(c.split()) >= 2 and len(c) <= 45:
            cleaned.append(c)
    return list(dict.fromkeys(cleaned))[:6]


def detect_positions(text):
    low = text.lower()
    return [pos for pos, words in POSITION_WORDS.items() if any(w in low for w in words)]


def confidence_label(score):
    if score >= 85:
        return "Çok Yüksek"
    if score >= 80:
        return "Yüksek"
    if score >= 60:
        return "Orta"
    return "Düşük"


def calculate_score(row, duplicate_count=1, watchlist=None):
    watchlist = watchlist or []
    text = row["raw_text"]
    score = int(row["weight"])

    keyword_hits = sum(1 for k in TRANSFER_KEYWORDS if k in text)
    score += min(keyword_hits * 2, 16)

    if any(w in text for w in HIGH_CONFIDENCE_WORDS):
        score += 18

    if any(w in text for w in LOW_CONFIDENCE_WORDS):
        score -= 14

    if any(exp in text for exp in EXPERT_NAMES):
        score += 13

    if "here we go" in text:
        score += 25

    if "kap.org.tr" in row["link"] or "galatasaray.org" in row["link"]:
        score = max(score, 96)

    if any(player.lower() in text for player in watchlist):
        score += 8

    score += min(max(duplicate_count - 1, 0) * 8, 24)
    return max(1, min(score, 99))


def alarm_type(text, score):
    low = text.lower()
    if "here we go" in low:
        return "🚨 HERE WE GO ALARMI"
    if "kap" in low or "galatasaray.org" in low or "official" in low or "resmi" in low:
        return "✅ RESMİ/KAP KONTROL"
    if score >= 85:
        return "🔥 ÇOK GÜÇLÜ HABER"
    if score >= 80:
        return "🟢 YÜKSEK GÜVEN"
    return ""


def prepare_data(rows, watchlist):
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates(subset=["id"])
    df["players"] = df["display_text"].apply(extract_players)
    df["positions"] = df["display_text"].apply(detect_positions)

    player_counts = {}
    for players in df["players"]:
        for p in players:
            player_counts[p] = player_counts.get(p, 0) + 1

    def dupe(players):
        if not players:
            return 1
        return max(player_counts.get(p, 1) for p in players)

    df["duplicate_count"] = df["players"].apply(dupe)
    df["score"] = df.apply(lambda r: calculate_score(r, r["duplicate_count"], watchlist), axis=1)
    df["confidence"] = df["score"].apply(confidence_label)
    df["players_text"] = df["players"].apply(lambda x: ", ".join(x) if x else "-")
    df["positions_text"] = df["positions"].apply(lambda x: ", ".join(x) if x else "-")
    df["alarm"] = df.apply(lambda r: alarm_type(r["display_text"], int(r["score"])), axis=1)
    return df.sort_values("score", ascending=False)


def save_news(df):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for _, r in df.iterrows():
        cur.execute("SELECT id FROM news WHERE id=?", (r["id"],))
        exists = cur.fetchone()
        if exists:
            cur.execute("""
                UPDATE news SET score=?, confidence=?, players=?, positions=?, alarm=?, last_seen=?
                WHERE id=?
            """, (int(r["score"]), r["confidence"], r["players_text"], r["positions_text"], r["alarm"], now, r["id"]))
        else:
            cur.execute("""
                INSERT INTO news
                (id,title,link,source,source_type,published,summary,score,confidence,players,positions,alarm,first_seen,last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r["id"], r["title"], r["link"], r["source"], r["source_type"], r["published"],
                r["summary"], int(r["score"]), r["confidence"], r["players_text"], r["positions_text"],
                r["alarm"], now, now
            ))
    con.commit()
    con.close()


def load_history():
    con = sqlite3.connect(DB_FILE)
    try:
        df = pd.read_sql_query("SELECT * FROM news ORDER BY last_seen DESC", con)
    except Exception:
        df = pd.DataFrame()
    con.close()
    return df


def score_class(score):
    if score >= 80:
        return "score-high"
    if score >= 60:
        return "score-mid"
    return "score-low"


def build_ai_summary(df):
    if df.empty:
        return "Şu an özetlenecek haber bulunamadı."

    high = df[df["score"] >= 80]
    top_players = []
    exploded = df[df["players_text"] != "-"].copy()
    if not exploded.empty:
        exploded = exploded.assign(player=exploded["players_text"].str.split(", ")).explode("player")
        top_players = exploded.groupby("player").size().sort_values(ascending=False).head(5).index.tolist()

    lines = []
    lines.append(f"Bugün radar sisteminde {len(df)} haber tarandı. {len(high)} haber 80+ yüksek güven seviyesine çıktı.")
    if top_players:
        lines.append("En çok öne çıkan isimler: " + ", ".join(top_players) + ".")
    if not high.empty:
        best = high.iloc[0]
        lines.append(f"En güçlü haber: “{best['title']}” — kaynak: {best['source']}, güven puanı: {int(best['score'])}/99.")
    lines.append("Not: Bu sistem kesin transfer bilgisi vermez; kaynak kalitesi, tekrar sayısı ve haber diline göre olasılık sıralaması yapar.")
    return "\n\n".join(lines)


def main():
    init_db()
    sources = load_json(CONFIG_FILE, [])
    watchlist_data = load_json(WATCHLIST_FILE, {"players": ["Victor Osimhen", "Hakan Calhanoglu", "Ederson", "Bernardo Silva", "Can Uzun"]})
    watchlist = watchlist_data.get("players", [])

    st.markdown("# 🟡🔴 Galatasaray Transfer Radar Merkezi Plus")
    st.markdown("Tek linkten çalışan, 5 dakikada bir yenilenen, yerli/yabancı haberleri puanlayan transfer takip paneli.")

    with st.sidebar:
        st.header("Kontrol Paneli")
        auto_refresh = st.checkbox("5 dakikada bir otomatik yenile", value=True)
        refresh_seconds = st.slider("Yenileme süresi", 60, 900, 300, step=60)
        threshold = st.slider("Minimum güven puanı", 1, 99, 55)
        only_high = st.checkbox("Sadece 80+ yüksek güven", value=False)
        show_count = st.slider("Haber sayısı", 10, 150, 60)

        st.divider()
        st.subheader("Özel Oyuncu Takibi")
        new_watch = st.text_area("Takip edilecek oyuncular", "\n".join(watchlist), height=150)
        if st.button("Takip listesini kaydet"):
            players = [x.strip() for x in new_watch.splitlines() if x.strip()]
            save_json(WATCHLIST_FILE, {"players": players})
            st.success("Takip listesi kaydedildi.")
            st.rerun()

        st.divider()
        st.caption(f"Kaynak sayısı: {len(sources)}")

    if auto_refresh:
        st_autorefresh(interval=refresh_seconds * 1000, key="auto_refresh")

    all_rows, errors = [], []
    for source in sources:
        try:
            all_rows.extend(fetch_feed(source))
        except Exception as ex:
            errors.append(f"{source.get('name','Kaynak')}: {ex}")

    df = prepare_data(all_rows, watchlist)
    if not df.empty:
        save_news(df)
    history = load_history()

    if df.empty:
        st.error("Haber çekilemedi. Kaynakları veya internet bağlantısını kontrol edin.")
        return

    view = df[df["score"] >= (80 if only_high else threshold)].head(show_count)
    alarms = df[df["alarm"] != ""].head(10)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Anlık Haber", len(df))
    c2.metric("80+ Güven", int((df["score"] >= 80).sum()))
    c3.metric("Alarm", len(alarms))
    c4.metric("Arşiv", len(history))

    st.subheader("🧠 Günlük Transfer Özeti")
    st.info(build_ai_summary(df))

    if not alarms.empty:
        st.subheader("🚨 Alarm Merkezi")
        for _, r in alarms.iterrows():
            st.markdown(f"""
            <div class="alert-card">
                <span class="badge">{r['alarm']}</span>
                <h3><a href="{r['link']}" target="_blank">{r['title']}</a></h3>
                <div class="small"><b>Kaynak:</b> {r['source']} | <b>Oyuncu:</b> {r['players_text']}</div>
                <h4 class="{score_class(int(r['score']))}">Güven Puanı: {int(r['score'])}/99 - {r['confidence']}</h4>
            </div>
            """, unsafe_allow_html=True)

    st.subheader("🔥 Oyuncu Bazlı Sıcak Liste")
    exploded = df[df["players_text"] != "-"].copy()
    if not exploded.empty:
        exploded = exploded.assign(player=exploded["players_text"].str.split(", ")).explode("player")
        hot = (
            exploded.groupby("player")
            .agg(
                haber_sayisi=("title", "count"),
                en_yuksek_puan=("score", "max"),
                kaynaklar=("source", lambda x: ", ".join(sorted(set(x))[:5]))
            )
            .reset_index()
            .sort_values(["en_yuksek_puan", "haber_sayisi"], ascending=False)
            .head(20)
        )
        st.dataframe(hot, use_container_width=True, hide_index=True)

    st.subheader("✅ En Güvenilir Haberler")
    for _, r in view.iterrows():
        st.markdown(f"""
        <div class="news-card">
            <h3><a href="{r['link']}" target="_blank">{r['title']}</a></h3>
            <div class="small"><b>Kaynak:</b> {r['source']} | <b>Oyuncu:</b> {r['players_text']} | <b>Mevki:</b> {r['positions_text']}</div>
            <h4 class="{score_class(int(r['score']))}">Güven Puanı: {int(r['score'])}/99 - {r['confidence']}</h4>
            <p>{(r['summary'] or '')[:420]}</p>
        </div>
        """, unsafe_allow_html=True)

    st.subheader("📊 Haber Tablosu")
    table = view[["score", "confidence", "alarm", "source", "players_text", "positions_text", "title", "link"]]
    st.dataframe(table, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        st.download_button("CSV indir", table.to_csv(index=False).encode("utf-8-sig"), "gs_transfer_radar.csv", "text/csv")
    with col2:
        excel_path = "gs_transfer_radar.xlsx"
        table.to_excel(excel_path, index=False)
        with open(excel_path, "rb") as f:
            st.download_button("Excel indir", f, excel_path)

    if errors:
        with st.expander("Okunamayan kaynaklar"):
            for e in errors:
                st.warning(e)


if __name__ == "__main__":
    main()

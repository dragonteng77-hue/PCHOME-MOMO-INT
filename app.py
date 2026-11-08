# -*- coding: utf-8 -*-
"""
PChome × MOMO 價格查詢與比價系統 (Streamlit)

執行方式:
    pip install -r requirements.txt
    streamlit run app.py

功能:
1. 三個搜尋分頁/搜尋框:PChome、MOMO、綜合(兩平台同時搜尋比價)
2. 每個搜尋框都可自訂「關鍵字」與「爬取頁數」
3. 綜合比價:兩平台統計、價格分布比較、相似商品配對與價差
4. 圖表(Plotly,瀏覽器渲染,免安裝中文字型):折線圖、圓餅圖、旭日圖、箱型圖
5. 結果快取 10 分鐘、可下載 CSV
"""
import re
import time
from datetime import datetime
from difflib import SequenceMatcher

import pandas as pd
import plotly.express as px
import streamlit as st
from curl_cffi import requests

st.set_page_config(page_title="PChome × MOMO 比價系統", page_icon="🛒", layout="wide")

# ----------------------------------------------------------------------
# 常數
# ----------------------------------------------------------------------
PCHOME_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_ITEM_URL = "https://24h.pchome.com.tw/prod/{}"
PCHOME_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://24h.pchome.com.tw/",
}
PCHOME_SORT = {
    "熱銷排序": "sale/dc",
    "價格由低到高": "prc/ac",
    "價格由高到低": "prc/dc",
    "相關度": "rnk/dc",
}

MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HOME = "https://www.momoshop.com.tw/"
MOMO_ITEM_URL = "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code={}"
MOMO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}

COLUMNS = ["平台", "頁數", "品名", "價格", "連結"]
PLATFORM_COLORS = {"PChome": "#1f77b4", "MOMO": "#e8590c"}


# ----------------------------------------------------------------------
# 爬蟲
# ----------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def fetch_pchome(keyword: str, pages: int, sort: str, _delay: float = 1.0):
    """爬取 PChome 24h,回傳 (DataFrame, 錯誤訊息列表)"""
    rows, errors = [], []
    session = requests.Session(impersonate="chrome")
    for page in range(1, pages + 1):
        try:
            resp = session.get(
                PCHOME_API,
                params={"q": keyword, "page": page, "sort": sort},
                headers=PCHOME_HEADERS,
                timeout=20,
            )
            if resp.status_code != 200:
                errors.append(f"[PChome] 第 {page} 頁 HTTP {resp.status_code}")
                continue
            data = resp.json()
            prods = data.get("prods") or []
            if not prods:
                errors.append(f"[PChome] 第 {page} 頁沒有資料,停止爬取")
                break
            for p in prods:
                pid = p.get("Id", "")
                rows.append(
                    {
                        "平台": "PChome",
                        "頁數": page,
                        "品名": p.get("name", ""),
                        "價格": p.get("price", ""),
                        "連結": PCHOME_ITEM_URL.format(pid) if pid else "",
                    }
                )
            total_page = data.get("totalPage")
            if total_page and page >= int(total_page):
                if page < pages:
                    errors.append(f"[PChome] 此關鍵字共 {total_page} 頁,已全部抓完")
                break
        except Exception as e:
            errors.append(f"[PChome] 第 {page} 頁發生錯誤:{e}")
        if page < pages:
            time.sleep(_delay)
    return clean_df(pd.DataFrame(rows, columns=COLUMNS)), errors


def _momo_payload(keyword: str, page: int) -> dict:
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


@st.cache_data(ttl=600, show_spinner=False)
def fetch_momo(keyword: str, pages: int, _delay: float = 1.0):
    """爬取 MOMO 購物網,回傳 (DataFrame, 錯誤訊息列表)"""
    rows, errors = [], []
    session = requests.Session(impersonate="chrome")  # 模擬 Chrome TLS 指紋
    try:
        session.get(MOMO_HOME, headers=MOMO_HEADERS, timeout=15)  # 先取得 cookie
    except Exception as e:
        errors.append(f"[MOMO] 取得首頁 cookie 失敗:{e}")

    for page in range(1, pages + 1):
        try:
            resp = session.post(
                MOMO_API, headers=MOMO_HEADERS, json=_momo_payload(keyword, page), timeout=20
            )
            if resp.status_code != 200:
                errors.append(f"[MOMO] 第 {page} 頁 HTTP {resp.status_code}")
                continue
            goods = resp.json().get("rtnSearchData", {}).get("goodsInfoList", [])
            if not goods:
                errors.append(f"[MOMO] 第 {page} 頁沒有資料,停止爬取")
                break
            for p in goods:
                code = p.get("goodsCode", "")
                rows.append(
                    {
                        "平台": "MOMO",
                        "頁數": page,
                        "品名": p.get("goodsName", ""),
                        "價格": p.get("goodsPrice", ""),
                        "連結": MOMO_ITEM_URL.format(code) if code else "",
                    }
                )
        except Exception as e:
            errors.append(f"[MOMO] 第 {page} 頁發生錯誤:{e}")
        if page < pages:
            time.sleep(_delay)
    return clean_df(pd.DataFrame(rows, columns=COLUMNS)), errors


# ----------------------------------------------------------------------
# 資料整理
# ----------------------------------------------------------------------
def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """價格轉整數、移除無效值與重複商品"""
    if df.empty:
        return df
    df = df.copy()
    df["價格"] = pd.to_numeric(
        df["價格"].astype(str).str.replace(r"[^\d.]", "", regex=True), errors="coerce"
    )
    df = df.dropna(subset=["價格"])
    df["價格"] = df["價格"].astype(int)
    df = df[(df["價格"] > 0) & (df["品名"].astype(str).str.strip() != "")]
    return df.drop_duplicates(subset=["平台", "品名", "價格"]).reset_index(drop=True)


def guess_brand(name: str) -> str:
    """從商品名稱粗略抓品牌:優先取【品牌】,否則取第一個詞"""
    m = re.search(r"【(.+?)】", str(name))
    if m:
        return m.group(1).strip()[:12] or "其他"
    first = re.split(r"[\s/｜|]", str(name).strip())[0]
    return first[:12] or "其他"


def add_price_bins(df: pd.DataFrame, bin_size: int) -> pd.DataFrame:
    df = df.copy()
    lower = (df["價格"] // bin_size) * bin_size
    df["價格區間"] = lower.astype(str) + "~" + (lower + bin_size - 1).astype(str)
    df["_排序"] = lower
    return df


def _norm(s: str) -> str:
    s = re.sub(r"[【】\[\]()()「」/｜|\-_,,、]", " ", str(s).lower())
    return re.sub(r"\s+", " ", s).strip()


def match_products(a: pd.DataFrame, b: pd.DataFrame, threshold: float, limit: int = 200):
    """以名稱相似度配對 PChome(a) 與 MOMO(b) 的相似商品,計算價差"""
    a, b = a.head(limit), b.head(limit)
    b_names = [_norm(n) for n in b["品名"]]
    out = []
    for _, ra in a.iterrows():
        na = _norm(ra["品名"])
        best_i, best_r = -1, 0.0
        for i, nb in enumerate(b_names):
            sm = SequenceMatcher(None, na, nb)
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            r = sm.ratio()
            if r > best_r:
                best_i, best_r = i, r
        if best_i >= 0 and best_r >= threshold:
            rb = b.iloc[best_i]
            diff = int(ra["價格"]) - int(rb["價格"])
            out.append(
                {
                    "PChome 品名": ra["品名"],
                    "PChome 價格": int(ra["價格"]),
                    "MOMO 品名": rb["品名"],
                    "MOMO 價格": int(rb["價格"]),
                    "價差(PChome−MOMO)": diff,
                    "較便宜": "PChome" if diff < 0 else ("MOMO" if diff > 0 else "相同"),
                    "相似度": round(best_r, 2),
                    "PChome 連結": ra["連結"],
                    "MOMO 連結": rb["連結"],
                }
            )
    res = pd.DataFrame(out)
    if not res.empty:
        res = res.sort_values("相似度", ascending=False).reset_index(drop=True)
    return res


# ----------------------------------------------------------------------
# 圖表
# ----------------------------------------------------------------------
def line_chart(df: pd.DataFrame, top_n: int):
    parts = []
    for _, g in df.groupby("平台", sort=False):
        g = g.head(top_n).reset_index(drop=True)
        g["商品序號"] = g.index + 1
        parts.append(g)
    plot_df = pd.concat(parts)
    fig = px.line(
        plot_df, x="商品序號", y="價格", color="平台", markers=True,
        hover_data=["品名"], color_discrete_map=PLATFORM_COLORS,
        title=f"價格折線圖(每平台前 {top_n} 筆)",
    )
    for plat, g in df.groupby("平台", sort=False):
        fig.add_hline(
            y=g["價格"].mean(), line_dash="dash",
            line_color=PLATFORM_COLORS.get(plat, "red"),
            annotation_text=f"{plat} 平均 {g['價格'].mean():,.0f}",
            annotation_position="top left",
        )
    return fig


def pie_charts(df: pd.DataFrame, bin_size: int):
    binned = add_price_bins(df, bin_size)
    plats = list(binned["平台"].unique())
    cols = st.columns(len(plats))
    for col, plat in zip(cols, plats):
        counts = (
            binned[binned["平台"] == plat]
            .groupby(["價格區間", "_排序"]).size().reset_index(name="商品數")
            .sort_values("_排序")
        )
        fig = px.pie(counts, names="價格區間", values="商品數", hole=0.3,
                     title=f"{plat} 價格區間占比")
        fig.update_traces(textposition="inside", textinfo="percent+label", sort=False)
        fig.update_layout(height=520)
        col.plotly_chart(fig, use_container_width=True)


def sunburst_chart(df: pd.DataFrame, bin_size: int):
    d = add_price_bins(df, bin_size)
    d["品牌"] = d["品名"].map(guess_brand)
    top = d["品牌"].value_counts().head(12).index  # 只保留常見品牌,避免圖太雜
    d["品牌"] = d["品牌"].where(d["品牌"].isin(top), "其他")
    multi = d["平台"].nunique() > 1
    path = (["平台"] if multi else []) + ["價格區間", "品牌"]
    g = d.groupby(path).agg(數量=("價格", "size"), 平均價=("價格", "mean")).reset_index()
    fig = px.sunburst(
        g, path=path, values="數量", color="平均價",
        color_continuous_scale="Blues", title="價格區間 → 品牌 旭日圖",
    )
    return fig


def box_chart(df: pd.DataFrame):
    fig = px.box(
        df, x="平台", y="價格", color="平台", points="all", hover_data=["品名"],
        color_discrete_map=PLATFORM_COLORS, title="價格分布箱型圖",
    )
    return fig


def range_bar_chart(df: pd.DataFrame, bin_size: int):
    binned = add_price_bins(df, bin_size)
    counts = (
        binned.groupby(["價格區間", "_排序", "平台"]).size().reset_index(name="商品數")
        .sort_values("_排序")
    )
    return px.bar(
        counts, x="價格區間", y="商品數", color="平台", barmode="group",
        color_discrete_map=PLATFORM_COLORS, title="各價格區間商品數比較",
    )


# ----------------------------------------------------------------------
# 結果呈現
# ----------------------------------------------------------------------
def show_metrics(df: pd.DataFrame):
    for plat, g in df.groupby("平台", sort=False):
        st.markdown(f"**{plat}**")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("商品數", f"{len(g):,}")
        c2.metric("平均價", f"${g['價格'].mean():,.0f}")
        c3.metric("中位數", f"${g['價格'].median():,.0f}")
        c4.metric("最高價", f"${g['價格'].max():,}")
        c5.metric("最低價", f"${g['價格'].min():,}")


def show_charts(df: pd.DataFrame, key: str, bin_size: int):
    multi = df["平台"].nunique() > 1
    options = ["折線圖", "圓餅圖", "旭日圖", "箱型圖"] + (["區間比較"] if multi else [])
    chart = st.radio("圖表類型", options, horizontal=True, key=f"{key}_chart")

    if chart == "折線圖":
        top_n = st.slider("每平台顯示筆數", 10, 200, 70, step=10, key=f"{key}_topn")
        fig = line_chart(df, top_n)
    elif chart == "圓餅圖":
        pie_charts(df, bin_size)
        return
    elif chart == "旭日圖":
        fig = sunburst_chart(df, bin_size)
        st.caption("品牌為依商品名稱【】或第一個詞推測,僅供參考。")
    elif chart == "箱型圖":
        fig = box_chart(df)
    else:
        fig = range_bar_chart(df, bin_size)

    fig.update_layout(height=600, font=dict(size=14))
    st.plotly_chart(fig, use_container_width=True)


def show_table(df: pd.DataFrame, key: str, name: str, kw: str):
    with st.expander("📋 查看資料表 / 下載", expanded=False):
        cfg = {"連結": st.column_config.LinkColumn("連結", display_text="開啟")}
        st.dataframe(df, use_container_width=True, column_config=cfg)
        st.download_button(
            "⬇️ 下載 CSV",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{name}_{kw}_{datetime.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
            key=f"{key}_dl",
        )


def show_single(result: dict, key: str, bin_size: int):
    df, kw = result["df"], result["keyword"]
    for msg in result["errors"]:
        st.warning(msg)
    if df.empty:
        st.error("沒有取得任何資料,可能被網站擋下或關鍵字無結果。")
        return
    st.subheader(f"「{kw}」查詢結果")
    show_metrics(df)
    show_charts(df, key, bin_size)
    show_table(df, key, df["平台"].iloc[0], kw)


def show_compare(result: dict, key: str, bin_size: int):
    df, kw = result["df"], result["keyword"]
    for msg in result["errors"]:
        st.warning(msg)
    if df.empty:
        st.error("兩個平台都沒有取得資料,可能被網站擋下或關鍵字無結果。")
        return
    if df["平台"].nunique() < 2:
        st.warning(f"只有 {df['平台'].iloc[0]} 有資料,無法完整比價。")

    st.subheader(f"「{kw}」兩平台比價")
    show_metrics(df)

    # 平台摘要比較
    summary = (
        df.groupby("平台")["價格"]
        .agg(商品數="count", 平均價="mean", 中位數="median", 最低價="min", 最高價="max")
        .round(0).astype(int)
    )
    if len(summary) == 2:
        cheaper = summary["平均價"].idxmin()
        gap = summary["平均價"].max() - summary["平均價"].min()
        st.success(f"整體平均價較低的平台:**{cheaper}**(平均價差約 ${gap:,})")
    st.dataframe(summary, use_container_width=True)

    tab_chart, tab_match = st.tabs(["📊 整體圖表", "🔍 相似商品配對比價"])
    with tab_chart:
        show_charts(df, key, bin_size)
    with tab_match:
        if df["平台"].nunique() < 2:
            st.info("需要兩個平台都有資料才能配對。")
        else:
            thr = st.slider("名稱相似度門檻", 0.4, 0.95, 0.6, 0.05, key=f"{key}_thr",
                            help="越高越嚴格,配對越準但筆數越少")
            a = df[df["平台"] == "PChome"]
            b = df[df["平台"] == "MOMO"]
            with st.spinner("配對中…"):
                matched = match_products(a, b, thr)
            if matched.empty:
                st.info("沒有符合門檻的相似商品,試著調低相似度。")
            else:
                wins = matched["較便宜"].value_counts()
                st.write(
                    f"共配對 **{len(matched)}** 組:"
                    + "、".join(f"{k} 較便宜 {v} 組" for k, v in wins.items())
                )
                cfg = {
                    "PChome 連結": st.column_config.LinkColumn("PChome 連結", display_text="開啟"),
                    "MOMO 連結": st.column_config.LinkColumn("MOMO 連結", display_text="開啟"),
                }
                st.dataframe(matched, use_container_width=True, column_config=cfg)
                st.download_button(
                    "⬇️ 下載配對結果 CSV",
                    data=matched.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"比價配對_{kw}_{datetime.now():%Y%m%d_%H%M}.csv",
                    mime="text/csv",
                    key=f"{key}_matchdl",
                )
            st.caption("配對依商品名稱相似度推測,可能有規格/型號不同的誤配,請以商品頁為準。")
    show_table(df, key, "比價", kw)


# ----------------------------------------------------------------------
# 介面
# ----------------------------------------------------------------------
st.title("🛒 PChome × MOMO 價格查詢與比價系統")
st.caption("三個搜尋框:PChome、MOMO、綜合比價。輸入關鍵字與爬取頁數即可查詢。")

with st.sidebar:
    st.header("共用設定")
    delay = st.slider("每頁間隔秒數", 0.0, 10.0, 1.0, 0.5,
                      help="太短可能被網站限制,建議 1 秒以上")
    bin_size = st.slider("價格區間大小(元)", 100, 5000, 500, step=100)
    pchome_sort_label = st.selectbox("PChome 排序方式", list(PCHOME_SORT.keys()))
    st.divider()
    if st.button("🗑️ 清除快取與結果", use_container_width=True):
        st.cache_data.clear()
        for k in ("res_pchome", "res_momo", "res_both"):
            st.session_state.pop(k, None)
        st.rerun()

pchome_sort = PCHOME_SORT[pchome_sort_label]
tab_p, tab_m, tab_b = st.tabs(["🔵 PChome", "🟠 MOMO", "⚖️ 綜合比價"])

# ---- PChome ----
with tab_p:
    with st.form("form_pchome"):
        c1, c2, c3 = st.columns([3, 1, 1])
        kw_p = c1.text_input("PChome 搜尋關鍵字", value="電風扇")
        pg_p = c2.number_input("爬取頁數", 1, 20, 1, key="pg_p")
        go_p = c3.form_submit_button("🔍 搜尋 PChome", use_container_width=True)
    if go_p:
        if not kw_p.strip():
            st.warning("請輸入商品關鍵字")
        else:
            with st.spinner(f"正在爬取 PChome「{kw_p}」前 {pg_p} 頁…"):
                d, e = fetch_pchome(kw_p.strip(), int(pg_p), pchome_sort, delay)
            st.session_state["res_pchome"] = {"keyword": kw_p.strip(), "df": d, "errors": e}
    if "res_pchome" in st.session_state:
        show_single(st.session_state["res_pchome"], "pchome", bin_size)
    else:
        st.info("請輸入條件後按「搜尋 PChome」。")

# ---- MOMO ----
with tab_m:
    with st.form("form_momo"):
        c1, c2, c3 = st.columns([3, 1, 1])
        kw_m = c1.text_input("MOMO 搜尋關鍵字", value="行動電源")
        pg_m = c2.number_input("爬取頁數", 1, 20, 3, key="pg_m")
        go_m = c3.form_submit_button("🔍 搜尋 MOMO", use_container_width=True)
    if go_m:
        if not kw_m.strip():
            st.warning("請輸入商品關鍵字")
        else:
            with st.spinner(f"正在爬取 MOMO「{kw_m}」前 {pg_m} 頁…"):
                d, e = fetch_momo(kw_m.strip(), int(pg_m), delay)
            st.session_state["res_momo"] = {"keyword": kw_m.strip(), "df": d, "errors": e}
    if "res_momo" in st.session_state:
        show_single(st.session_state["res_momo"], "momo", bin_size)
    else:
        st.info("請輸入條件後按「搜尋 MOMO」。")

# ---- 綜合比價 ----
with tab_b:
    with st.form("form_both"):
        c1, c2, c3 = st.columns([3, 1, 1])
        kw_b = c1.text_input("綜合搜尋關鍵字(兩平台同時搜尋)", value="行動電源")
        pg_b = c2.number_input("每平台爬取頁數", 1, 20, 2, key="pg_b")
        go_b = c3.form_submit_button("⚖️ 開始比價", use_container_width=True)
    if go_b:
        if not kw_b.strip():
            st.warning("請輸入商品關鍵字")
        else:
            k, n = kw_b.strip(), int(pg_b)
            errors, frames = [], []
            with st.status(f"正在比價「{k}」…", expanded=True) as status:
                st.write("爬取 PChome…")
                dp, ep = fetch_pchome(k, n, pchome_sort, delay)
                st.write(f"PChome:{len(dp)} 筆")
                st.write("爬取 MOMO…")
                dm, em = fetch_momo(k, n, delay)
                st.write(f"MOMO:{len(dm)} 筆")
                status.update(label="爬取完成", state="complete", expanded=False)
            frames = [x for x in (dp, dm) if not x.empty]
            both = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
            st.session_state["res_both"] = {"keyword": k, "df": both, "errors": ep + em}
    if "res_both" in st.session_state:
        show_compare(st.session_state["res_both"], "both", bin_size)
    else:
        st.info("請輸入條件後按「開始比價」。")

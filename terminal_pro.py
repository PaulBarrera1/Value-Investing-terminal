"""
╔══════════════════════════════════════════════════════════════════╗
║        TERMINAL FINANCIERA  — Dashboard Institucional         ║
║  Fuentes: Alpha Vantage (fundamentales 10 años) + yfinance       ║
║  Autor  : Paul Barrera Full-Stack / Quant Finance refactor             ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import time
import json
import threading
import webbrowser
import requests
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import yfinance as yf

# ──────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN GLOBAL
# ──────────────────────────────────────────────────────────────────

AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")  # Alpha Vantage API Key (env var)
AV_BASE = "https://www.alphavantage.co/query"
AV_DELAY = 12                          # segundos entre calls (plan free: 5 req/min)

DARK = {
    "bg":      "#0d1117",
    "panel":   "#161b22",
    "border":  "#30363d",
    "accent":  "#58a6ff",
    "green":   "#3fb950",
    "red":     "#f85149",
    "yellow":  "#d29922",
    "text":    "#e6edf3",
    "muted":   "#8b949e",
    "purple":  "#bc8cff",
    "orange":  "#f0883e",
    "teal":    "#39d353",
}

# Paleta para gráficos comparativos (distinguible en fondo oscuro)
PALETTE = ["#58a6ff", "#3fb950", "#f0883e", "#bc8cff", "#39d353",
           "#d29922", "#f85149", "#79c0ff", "#56d364", "#ffa657"]


# ──────────────────────────────────────────────────────────────────
#  CAPA DE ACCESO A ALPHA VANTAGE (con rate-limit handling)
# ──────────────────────────────────────────────────────────────────

def av_request(function: str, symbol: str, extra: dict = None) -> dict:
    """
    Hace una petición a Alpha Vantage con reintentos ante rate-limit.
    Alpha Vantage Free: 25 req/día, 5 req/minuto → esperamos AV_DELAY seg entre calls.
    """
    params = {"function": function, "symbol": symbol, "apikey": AV_KEY}
    if extra:
        params.update(extra)

    for intento in range(3):
        try:
            r = requests.get(AV_BASE, params=params, timeout=15)
            data = r.json()

            # Detectar rate-limit o error de API
            if "Note" in data or "Information" in data:
                msg = data.get("Note") or data.get("Information", "")
                print(f"   ⚠️  Rate-limit AV ({function}): {msg[:80]}… esperando {AV_DELAY}s")
                time.sleep(AV_DELAY)
                continue

            return data
        except Exception as e:
            print(f"   ❌ Error en {function} (intento {intento+1}): {e}")
            time.sleep(5)

    return {}


def av_json_a_df(data: dict, report_key: str, date_col: str = "fiscalDateEnding") -> pd.DataFrame:
    """
    Convierte el JSON de AV (annualReports / quarterlyReports) a DataFrame.
    Convierte columnas numéricas; 'None' → NaN.
    """
    registros = data.get(report_key, [])
    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros)
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).reset_index(drop=True)

    # Convertir todo lo convertible a numérico
    for col in df.columns:
        if col != date_col:
            df[col] = pd.to_numeric(df[col].replace("None", np.nan), errors="coerce")

    return df


# ──────────────────────────────────────────────────────────────────
#  OBTENCIÓN DE DATOS (orquestador principal)
# ──────────────────────────────────────────────────────────────────

def obtener_todos_los_datos(ticker: str, periodo_precio: str = "2y") -> dict:
    """
    Descarga y estructura TODOS los datos necesarios:
      - Precios históricos (yfinance)
      - Overview corporativo (AV)
      - Income Statement anual + trimestral (AV)
      - Balance Sheet anual + trimestral (AV)
      - Cash Flow anual + trimestral (AV)
      - Earnings sorpresa (yfinance)
    """
    print(f"\n{'─'*55}")
    print(f"  Descargando datos para {ticker}")
    print(f"{'─'*55}")

    resultado = {"ticker": ticker}

    # ── 1. Precios e info básica (yfinance, sin límite) ─────────
    print("  [1/6] Precios históricos (yfinance)…")
    yf_ticker = yf.Ticker(ticker)
    resultado["hist"]         = yf_ticker.history(period=periodo_precio)
    resultado["info"]         = yf_ticker.info or {}
    resultado["earnings_hist"] = _safe_yf(yf_ticker, "earnings_history")
    resultado["earnings_cal"]  = _safe_yf(yf_ticker, "earnings_dates")

    # ── 2. Overview AV ─────────────────────────────────────────
    print("  [2/6] Overview corporativo (Alpha Vantage)…")
    resultado["overview"] = av_request("OVERVIEW", ticker)
    time.sleep(AV_DELAY)

    # ── 3. Income Statement ─────────────────────────────────────
    print("  [3/6] Income Statement (Alpha Vantage)…")
    raw_is = av_request("INCOME_STATEMENT", ticker)
    resultado["is_anual"]  = av_json_a_df(raw_is, "annualReports")
    resultado["is_trim"]   = av_json_a_df(raw_is, "quarterlyReports")
    time.sleep(AV_DELAY)

    # ── 4. Balance Sheet ────────────────────────────────────────
    print("  [4/6] Balance Sheet (Alpha Vantage)…")
    raw_bs = av_request("BALANCE_SHEET", ticker)
    resultado["bs_anual"]  = av_json_a_df(raw_bs, "annualReports")
    resultado["bs_trim"]   = av_json_a_df(raw_bs, "quarterlyReports")
    time.sleep(AV_DELAY)

    # ── 5. Cash Flow ────────────────────────────────────────────
    print("  [5/6] Cash Flow (Alpha Vantage)…")
    raw_cf = av_request("CASH_FLOW", ticker)
    resultado["cf_anual"]  = av_json_a_df(raw_cf, "annualReports")
    resultado["cf_trim"]   = av_json_a_df(raw_cf, "quarterlyReports")
    time.sleep(AV_DELAY)

    # ── 6. Earnings Call links ──────────────────────────────────
    print("  [6/6] Preparando links de transcripciones…")
    resultado["transcripts_links"] = {
        "The Motley Fool":
            f"https://www.fool.com/quote/{ticker.lower()}/#quote-earnings-transcripts",
        "Seeking Alpha":
            f"https://seekingalpha.com/symbol/{ticker}/earnings/transcripts",
        "AlphaStreet":
            f"https://alphastreet.com/company/{ticker}/transcripts",
        "SEC EDGAR (8-K)":
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2019-01-01&forms=8-K",
    }

    print(f"\n  ✅ Datos completos para {ticker}\n")
    return resultado


def _safe_yf(obj, attr):
    """Intenta obtener un atributo de yfinance sin crashear."""
    try:
        return getattr(obj, attr)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────
#  CÁLCULO DE MÉTRICAS FINANCIERAS
# ──────────────────────────────────────────────────────────────────

def calcular_margenes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Análisis Vertical: calcula márgenes como % del Revenue.
    Fórmulas:
      Margen Bruto    = Gross Profit / Total Revenue
      Margen Operativo= Operating Income / Total Revenue
      Margen Neto     = Net Income / Total Revenue
      R&D %           = R&D Expense / Total Revenue
    """
    out = pd.DataFrame()
    rev = df.get("totalRevenue", pd.Series(dtype=float))
    out["fecha"]           = df["fiscalDateEnding"]
    out["revenue"]         = rev
    out["gross_profit"]    = df.get("grossProfit", np.nan)
    out["operating_inc"]   = df.get("operatingIncome", np.nan)
    out["net_income"]      = df.get("netIncome", np.nan)
    out["rd_expense"]      = df.get("researchAndDevelopment", np.nan)
    out["sga_expense"]     = df.get("sellingGeneralAndAdministrative", np.nan)
    out["ebitda"]          = df.get("ebitda", np.nan)
    out["cost_of_revenue"] = df.get("costOfRevenue", np.nan)

    # Márgenes (análisis vertical)
    for campo, col in [
        ("margen_bruto",    "gross_profit"),
        ("margen_operativo","operating_inc"),
        ("margen_neto",     "net_income"),
        ("rd_pct",          "rd_expense"),
        ("sga_pct",         "sga_expense"),
    ]:
        out[campo] = (out[col] / rev * 100).round(2)

    # Crecimiento YoY (análisis horizontal)
    for col in ["revenue", "gross_profit", "net_income", "ebitda"]:
        out[f"{col}_yoy"] = out[col].pct_change() * 100

    return out.reset_index(drop=True)


def calcular_ratios_balance(bs: pd.DataFrame, is_: pd.DataFrame) -> pd.DataFrame:
    """
    Ratios de solidez financiera:
      Deuda/Patrimonio   = Total Long-Term Debt / Total Shareholder Equity
      Current Ratio      = Total Current Assets / Total Current Liabilities
      Deuda Neta         = Deuda Total − Efectivo
    """
    out = pd.DataFrame()
    out["fecha"]              = bs["fiscalDateEnding"]
    out["total_assets"]       = bs.get("totalAssets", np.nan)
    out["current_assets"]     = bs.get("totalCurrentAssets", np.nan)
    out["non_current_assets"] = out["total_assets"] - out["current_assets"]
    out["current_liab"]       = bs.get("totalCurrentLiabilities", np.nan)
    out["total_liab"]         = bs.get("totalLiabilities", np.nan)
    out["equity"]             = bs.get("totalShareholderEquity", np.nan)
    out["long_term_debt"]     = bs.get("longTermDebt", np.nan)
    out["cash"]               = bs.get("cashAndCashEquivalentsAtCarryingValue", np.nan)
    out["inventory"]          = bs.get("inventory", np.nan)

    out["deuda_patrimonio"]   = (out["long_term_debt"] / out["equity"]).round(2)
    out["current_ratio"]      = (out["current_assets"] / out["current_liab"]).round(2)
    out["deuda_neta"]         = out["long_term_debt"] - out["cash"]

    return out.reset_index(drop=True)


def calcular_flujo_libre(cf: pd.DataFrame) -> pd.DataFrame:
    """
    Free Cash Flow = Operating Cash Flow − Capital Expenditures
    FCF Yield indica qué tanto efectivo genera la empresa vs su inversión en activos.
    """
    out = pd.DataFrame()
    out["fecha"]       = cf["fiscalDateEnding"]
    out["op_cashflow"] = cf.get("operatingCashflow", np.nan)
    out["inv_cashflow"] = cf.get("cashflowFromInvestment", np.nan)
    out["fin_cashflow"] = cf.get("cashflowFromFinancing", np.nan)
    capex_raw          = cf.get("capitalExpenditures", np.nan)
    # CAPEX viene negativo en AV; lo hacemos absoluto para la resta
    out["capex"]       = pd.to_numeric(capex_raw, errors="coerce").abs()
    out["fcf"]         = out["op_cashflow"] - out["capex"]
    return out.reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────
#  GENERADORES DE GRÁFICOS PLOTLY
# ──────────────────────────────────────────────────────────────────

LAYOUT_BASE = dict(
    template="plotly_dark",
    paper_bgcolor=DARK["bg"],
    plot_bgcolor=DARK["panel"],
    font=dict(family="IBM Plex Mono, monospace", color=DARK["text"], size=11),
    legend=dict(orientation="h", y=-0.18, bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=60, r=20, t=50, b=80),
    height=420,
)

def _layout(**overrides) -> dict:
    """Copia LAYOUT_BASE y aplica overrides sin TypeError por claves duplicadas."""
    base = LAYOUT_BASE.copy()
    base.update(overrides)
    return base


def _anos(df_m: pd.DataFrame) -> list:
    """Extrae lista de años cortos para labels del eje X."""
    return [str(f.year) for f in df_m["fecha"]]


def grafico_revenue_desglose(m: pd.DataFrame) -> go.Figure:
    """
    INCOME STATEMENT — Gráfico 1:
    Barras apiladas: Cost of Revenue vs Gross Profit
    + línea de Margen Bruto % (eje Y derecho).
    Muestra la capacidad de la empresa para convertir ventas en utilidad bruta.
    """
    años = _anos(m)
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=años, y=m["cost_of_revenue"] / 1e9,
        name="Costo de Ventas", marker_color=DARK["red"],
        hovertemplate="Año: %{x}<br>Costo: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Bar(
        x=años, y=m["gross_profit"] / 1e9,
        name="Utilidad Bruta", marker_color=DARK["green"],
        hovertemplate="Año: %{x}<br>Ut.Bruta: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=años, y=m["margen_bruto"],
        name="Margen Bruto %", mode="lines+markers",
        line=dict(color=DARK["yellow"], width=2.5),
        marker=dict(size=7),
        hovertemplate="Año: %{x}<br>Margen: %{y:.1f}%<extra></extra>",
    ), secondary_y=True)

    fig.update_layout(
        **_layout(),
        title="Revenue: Costo vs Utilidad Bruta",
        barmode="stack",
        yaxis_title="Monto (USD Billones)",
        yaxis2_title="Margen Bruto %",
    )
    return fig


def grafico_gastos_operativos(m: pd.DataFrame) -> go.Figure:
    """
    INCOME STATEMENT — Gráfico 2:
    Barras apiladas de gastos operativos: R&D + SG&A
    + líneas de Margen Operativo y Margen Neto.
    Revela la estructura de costos y palanca operativa.
    """
    años = _anos(m)
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=años, y=m["rd_expense"] / 1e9,
        name="I+D (R&D)", marker_color=DARK["purple"],
        hovertemplate="Año: %{x}<br>R&D: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Bar(
        x=años, y=m["sga_expense"] / 1e9,
        name="SG&A", marker_color=DARK["orange"],
        hovertemplate="Año: %{x}<br>SG&A: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=años, y=m["margen_operativo"],
        name="Mg. Operativo %", mode="lines+markers",
        line=dict(color=DARK["accent"], width=2.5),
        hovertemplate="Año: %{x}<br>Mg.Op: %{y:.1f}%<extra></extra>",
    ), secondary_y=True)

    fig.add_trace(go.Scatter(
        x=años, y=m["margen_neto"],
        name="Mg. Neto %", mode="lines+markers",
        line=dict(color=DARK["teal"], width=2.5, dash="dot"),
        hovertemplate="Año: %{x}<br>Mg.Neto: %{y:.1f}%<extra></extra>",
    ), secondary_y=True)

    fig.update_layout(
        **_layout(),
        title="Gastos Operativos: R&D + SG&A vs Márgenes",
        barmode="stack",
        yaxis_title="Monto (USD Billones)",
        yaxis2_title="Margen %",
    )
    return fig


def grafico_crecimiento_yoy(m: pd.DataFrame) -> go.Figure:
    """
    INCOME STATEMENT — Gráfico 3:
    Barras de crecimiento YoY (%) para Revenue, Utilidad Bruta y EBITDA.
    Análisis Horizontal: detecta aceleración o desaceleración del negocio.
    """
    años = _anos(m)
    fig = go.Figure()

    for campo, nombre, color in [
        ("revenue_yoy",      "Revenue",       DARK["accent"]),
        ("gross_profit_yoy", "Ut. Bruta",     DARK["green"]),
        ("net_income_yoy",   "Ut. Neta",      DARK["purple"]),
        ("ebitda_yoy",       "EBITDA",        DARK["yellow"]),
    ]:
        vals = m[campo]
        colores = [DARK["green"] if v >= 0 else DARK["red"] for v in vals.fillna(0)]
        fig.add_trace(go.Bar(
            x=años, y=vals, name=nombre,
            marker_color=color, opacity=0.85,
            hovertemplate=f"{nombre} YoY: %{{y:.1f}}%<extra></extra>",
        ))

    fig.add_hline(y=0, line_color=DARK["muted"], line_width=1)
    fig.update_layout(**_layout(), title="Crecimiento Año a Año (YoY %)",
                      yaxis_title="Crecimiento (%)", barmode="group")
    return fig


def grafico_composicion_activos(r: pd.DataFrame) -> go.Figure:
    """
    BALANCE SHEET — Gráfico 1:
    Barras apiladas al 100%: Activos Corrientes vs No Corrientes.
    Muestra qué tan líquida es la empresa y cómo evoluciona su estructura de activos.
    """
    años = _anos(r)
    total = r["current_assets"] + r["non_current_assets"]

    pct_corr   = (r["current_assets"]     / total * 100).round(1)
    pct_no_corr = (r["non_current_assets"] / total * 100).round(1)

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=años, y=r["current_assets"] / 1e9,
        name="Activos Corrientes", marker_color=DARK["green"],
        hovertemplate="Año: %{x}<br>Corrientes: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Bar(
        x=años, y=r["non_current_assets"] / 1e9,
        name="Activos No Corrientes", marker_color=DARK["accent"],
        hovertemplate="Año: %{x}<br>No Corrientes: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=años, y=r["current_ratio"],
        name="Current Ratio", mode="lines+markers",
        line=dict(color=DARK["yellow"], width=2.5),
        hovertemplate="Current Ratio: %{y:.2f}x<extra></extra>",
    ), secondary_y=True)

    fig.update_layout(
        **_layout(),
        title="Composición de Activos + Current Ratio",
        barmode="stack",
        yaxis_title="Monto (USD Billones)",
        yaxis2_title="Current Ratio (x)",
    )
    return fig


def grafico_estructura_capital(r: pd.DataFrame) -> go.Figure:
    """
    BALANCE SHEET — Gráfico 2:
    Barras agrupadas: Deuda LP vs Patrimonio Neto
    + línea Deuda/Patrimonio.
    Muestra el apalancamiento financiero y riesgo de solvencia.
    """
    años = _anos(r)
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=años, y=r["long_term_debt"] / 1e9,
        name="Deuda Largo Plazo", marker_color=DARK["red"],
        hovertemplate="Deuda LP: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Bar(
        x=años, y=r["equity"] / 1e9,
        name="Patrimonio Neto", marker_color=DARK["green"],
        hovertemplate="Patrimonio: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=años, y=r["deuda_neta"] / 1e9,
        name="Deuda Neta", mode="lines+markers",
        line=dict(color=DARK["orange"], width=2, dash="dot"),
        hovertemplate="Deuda Neta: $%{y:.2f}B<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=años, y=r["deuda_patrimonio"],
        name="D/E Ratio", mode="lines+markers",
        line=dict(color=DARK["yellow"], width=2.5),
        hovertemplate="D/E: %{y:.2f}x<extra></extra>",
    ), secondary_y=True)

    fig.update_layout(
        **_layout(),
        title="Estructura de Capital: Deuda vs Patrimonio",
        barmode="group",
        yaxis_title="Monto (USD Billones)",
        yaxis2_title="Ratio Deuda/Patrimonio (x)",
    )
    return fig


def grafico_waterfall_cashflow(f: pd.DataFrame, año_idx: int = -1) -> go.Figure:
    """
    CASH FLOW — Gráfico Cascada (Waterfall):
    Muestra la generación y uso del efectivo en el último año disponible.
    Operativo (entra) → Inversión (sale) → Financiamiento (sale/entra) → FCF.
    """
    fila = f.iloc[año_idx]
    año  = str(fila["fecha"].year)

    medidas = ["relative", "relative", "relative", "total"]
    valores = [
        fila["op_cashflow"],
        fila["inv_cashflow"],
        fila["fin_cashflow"],
        fila["fcf"],
    ]
    etiquetas = ["Flujo Operativo", "Flujo Inversión", "Flujo Financiamiento", "Free Cash Flow"]
    colores   = [
        DARK["green"] if v >= 0 else DARK["red"]
        for v in valores
    ]

    fig = go.Figure(go.Waterfall(
        name=año,
        orientation="v",
        measure=medidas,
        x=etiquetas,
        y=[v / 1e9 for v in valores],
        connector={"line": {"color": DARK["border"]}},
        increasing={"marker": {"color": DARK["green"]}},
        decreasing={"marker": {"color": DARK["red"]}},
        totals={"marker":    {"color": DARK["accent"]}},
        texttemplate="%{y:.2f}B",
        textposition="outside",
    ))

    fig.update_layout(**_layout(), title=f"Cascada de Flujo de Caja — {año}",
                      yaxis_title="USD Billones")
    return fig


def grafico_cashflow_series(f: pd.DataFrame) -> go.Figure:
    """
    CASH FLOW — Líneas comparativas multi-año:
    Flujo Operativo, de Inversión, Financiamiento y FCF.
    Permite ver tendencias y detectar años de inversión masiva o recompras.
    """
    años = _anos(f)
    fig = go.Figure()

    series = [
        ("op_cashflow",  "Flujo Operativo",    DARK["green"],  "solid"),
        ("inv_cashflow", "Flujo Inversión",     DARK["red"],    "dot"),
        ("fin_cashflow", "Flujo Financiamiento",DARK["orange"], "dash"),
        ("fcf",          "Free Cash Flow",      DARK["accent"], "solid"),
    ]

    for campo, nombre, color, dash in series:
        fig.add_trace(go.Scatter(
            x=años, y=f[campo] / 1e9,
            name=nombre, mode="lines+markers",
            line=dict(color=color, width=2.5, dash=dash),
            marker=dict(size=7),
            hovertemplate=f"{nombre}: $%{{y:.2f}}B<extra></extra>",
        ))

    fig.add_hline(y=0, line_color=DARK["muted"], line_width=1)
    fig.update_layout(**_layout(), title="Flujos de Caja: Serie Histórica",
                      yaxis_title="USD Billones")
    return fig


def grafico_precios(hist: pd.DataFrame, ticker: str, info: dict) -> go.Figure:
    """
    PRECIO — Candlestick + Volumen + MA50 + MA200.
    Configuración estándar de terminales Bloomberg / TradingView.
    """
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.75, 0.25], vertical_spacing=0.03,
    )

    fig.add_trace(go.Candlestick(
        x=hist.index, open=hist["Open"], high=hist["High"],
        low=hist["Low"],  close=hist["Close"],
        name="Precio",
        increasing_line_color=DARK["green"],
        decreasing_line_color=DARK["red"],
    ), row=1, col=1)

    for ventana, color, dash in [(50, DARK["yellow"], "dot"), (200, DARK["purple"], "dash")]:
        ma = hist["Close"].rolling(ventana).mean()
        fig.add_trace(go.Scatter(
            x=hist.index, y=ma, name=f"MA{ventana}",
            mode="lines", line=dict(color=color, width=1.5, dash=dash),
        ), row=1, col=1)

    colores_v = [
        DARK["green"] if hist["Close"].iloc[i] >= hist["Open"].iloc[i]
        else DARK["red"]
        for i in range(len(hist))
    ]
    fig.add_trace(go.Bar(
        x=hist.index, y=hist["Volume"],
        marker_color=colores_v, name="Volumen", opacity=0.65,
    ), row=2, col=1)

    nombre = info.get("longName", ticker)
    fig.update_layout(
        **_layout(
            height=560,
            title=f"{nombre} ({ticker}) — Precio histórico",
            xaxis_rangeslider_visible=False,
        ),
    )
    return fig


def grafico_ratios_valoracion(overview: dict, hist: pd.DataFrame) -> go.Figure:
    """
    VALORACIÓN — Gauge charts para P/E, P/B, EV/EBITDA y Dividend Yield.
    Benchmarks típicos del mercado como referencia.
    """
    metricas = [
        ("P/E Ratio",    "TrailingPE",     overview.get("TrailingPE",  "0"), 0, 50, 25),
        ("P/B Ratio",    "PriceToBookRatio",overview.get("PriceToBookRatio","0"), 0, 20, 3),
        ("EV/EBITDA",    "EVToEBITDA",     overview.get("EVToEBITDA",  "0"), 0, 40, 15),
        ("Div Yield %",  "DividendYield",  overview.get("DividendYield","0"), 0,  8, 2),
    ]

    fig = make_subplots(
        rows=1, cols=4,
        specs=[[{"type": "indicator"}]*4],
        subplot_titles=[m[0] for m in metricas],
    )

    for i, (nombre, key, val_str, vmin, vmax, referencia) in enumerate(metricas, 1):
        try:
            valor = float(val_str) * (100 if key == "DividendYield" else 1)
        except Exception:
            valor = 0

        fig.add_trace(go.Indicator(
            mode="gauge+number+delta",
            value=round(valor, 2),
            delta={"reference": referencia, "valueformat": ".1f"},
            gauge={
                "axis":      {"range": [vmin, vmax], "tickcolor": DARK["muted"]},
                "bar":       {"color": DARK["accent"]},
                "bgcolor":   DARK["panel"],
                "bordercolor": DARK["border"],
                "steps": [
                    {"range": [vmin, referencia*0.7],         "color": "rgba(63, 185, 80, 0.18)"},
                    {"range": [referencia*0.7, referencia*1.3],"color": "rgba(210, 153, 34, 0.18)"},
                    {"range": [referencia*1.3, vmax],          "color": "rgba(248, 81, 73, 0.18)"},
                ],
                "threshold": {
                    "line": {"color": DARK["yellow"], "width": 2},
                    "thickness": 0.75,
                    "value": referencia,
                },
            },
            number={"font": {"color": DARK["text"]}},
        ), row=1, col=i)

    fig.update_layout(
        **_layout(
            height=280,
            title="Ratios de Valoración (vs benchmark mercado)",
            margin=dict(l=20, r=20, t=60, b=20),
        ),
    )
    return fig


# ──────────────────────────────────────────────────────────────────
#  CONSTRUCCIÓN DEL HTML COMPLETO
# ──────────────────────────────────────────────────────────────────

def _fig_json(fig: go.Figure) -> str:
    """Serializa figura Plotly a JSON para insertar en HTML."""
    return fig.to_json()


def _metric_card(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<span class="m-sub">{sub}</span>' if sub else ""
    return f"""
    <div class="metric-card">
        <span class="m-label">{label}</span>
        <span class="m-val">{value}</span>
        {sub_html}
    </div>"""


def _tabla_html(df: pd.DataFrame, cols: list, titulo: str) -> str:
    """Renderiza un DataFrame como tabla HTML con las columnas indicadas."""
    if df is None or df.empty:
        return f"<p class='no-data'>Sin datos: {titulo}</p>"

    heads = "".join(f"<th>{c}</th>" for c in cols)
    rows  = ""
    for _, row in df.iterrows():
        cells = ""
        for c in cols:
            v = row.get(c, "")
            try:
                if isinstance(v, float) and not np.isnan(v):
                    txt = f"{v:,.2f}" if abs(v) < 1e9 else f"${v/1e9:.2f}B"
                elif hasattr(v, 'strftime'):          # Timestamp/datetime
                    txt = str(v)[:10]
                else:
                    txt = str(v)[:14] if v != "" else "—"
            except Exception:
                txt = str(v)[:14]
            cells += f"<td>{txt}</td>"
        rows += f"<tr>{cells}</tr>"

    return f"""
    <div class='table-wrap'>
        <p class='table-title'>{titulo}</p>
        <div class='table-scroll'>
            <table><thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table>
        </div>
    </div>"""


def construir_html(datos: dict, scanner_url: str = "") -> str:
    """Ensambla el dashboard HTML completo a partir de todos los datos."""
    ticker   = datos["ticker"]
    hist     = datos["hist"]
    info     = datos["info"]
    overview = datos.get("overview") or {}
    m        = calcular_margenes(datos["is_anual"])   if datos["is_anual"] is not None and not datos["is_anual"].empty else None
    r        = calcular_ratios_balance(datos["bs_anual"], datos["is_anual"]) if datos["bs_anual"] is not None and not datos["bs_anual"].empty else None
    cf       = calcular_flujo_libre(datos["cf_anual"]) if datos["cf_anual"] is not None and not datos["cf_anual"].empty else None

    nombre    = overview.get("Name") or info.get("longName", ticker)
    sector    = overview.get("Sector") or info.get("sector", "")
    industria = overview.get("Industry") or info.get("industry", "")
    descripcion = overview.get("Description", "Sin descripción disponible.")
    intercambio = overview.get("Exchange") or info.get("exchange", "")

    # ── Métricas del header ──────────────────────────────────────
    precio = hist["Close"].iloc[-1] if not hist.empty else 0
    var_pct = ((hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100) if len(hist) > 1 else 0
    signo = "+" if var_pct >= 0 else ""
    idea_defaults_json = json.dumps(
        {"ticker": ticker, "price": round(float(precio), 2)},
        ensure_ascii=True,
    ).replace("</", "<\\/")

    mkt_cap    = info.get("marketCap", 0)
    pe         = overview.get("TrailingPE", info.get("trailingPE", "N/A"))
    eps        = overview.get("EPS",        info.get("trailingEps", "N/A"))
    div_yield  = overview.get("DividendYield", info.get("dividendYield", "N/A"))
    w52h       = overview.get("52WeekHigh",  info.get("fiftyTwoWeekHigh", "N/A"))
    w52l       = overview.get("52WeekLow",   info.get("fiftyTwoWeekLow",  "N/A"))
    beta       = overview.get("Beta",        info.get("beta", "N/A"))
    analyst_tp = overview.get("AnalystTargetPrice", "N/A")

    def fv(v):
        try: return f"${float(v):.2f}"
        except: return str(v)

    def fp(v):
        try: return f"{float(v)*100:.2f}%"
        except: return str(v)

    header_cards = "".join([
        _metric_card("💰 Precio",       f"${precio:.2f}", f"{signo}{var_pct:.1f}% período"),
        _metric_card("🏢 Market Cap",   _fmt(mkt_cap)),
        _metric_card("📊 P/E Ratio",    fv(pe)),
        _metric_card("💵 EPS",          fv(eps)),
        _metric_card("💸 Div Yield",    fp(div_yield) if div_yield != "N/A" else "N/A"),
        _metric_card("📉 52W Mín",      fv(w52l)),
        _metric_card("📈 52W Máx",      fv(w52h)),
        _metric_card("🔄 Beta",         fv(beta)),
        _metric_card("🎯 Target Anal.", fv(analyst_tp)),
    ])

    # ── Gráficas ─────────────────────────────────────────────────
    fig_precio   = grafico_precios(hist, ticker, info)
    fig_ratios   = grafico_ratios_valoracion(overview, hist)

    # Income Statement
    has_is = m is not None and len(m) > 1
    fig_rev_js  = _fig_json(grafico_revenue_desglose(m))    if has_is else "null"
    fig_gastos_js= _fig_json(grafico_gastos_operativos(m))  if has_is else "null"
    fig_yoy_js  = _fig_json(grafico_crecimiento_yoy(m))     if has_is else "null"

    # Balance
    has_bs = r is not None and len(r) > 1
    fig_activos_js = _fig_json(grafico_composicion_activos(r)) if has_bs else "null"
    fig_cap_js     = _fig_json(grafico_estructura_capital(r))  if has_bs else "null"

    # Cash Flow
    has_cf = cf is not None and len(cf) > 1
    fig_wfall_js  = _fig_json(grafico_waterfall_cashflow(cf))  if has_cf else "null"
    fig_cfserie_js= _fig_json(grafico_cashflow_series(cf))     if has_cf else "null"

    fig_precio_js  = _fig_json(fig_precio)
    fig_ratios_js  = _fig_json(fig_ratios)

    # ── Tabla de márgenes ───────────────────────────────────────
    tabla_margenes = ""
    if has_is:
        cols_m = ["fecha","revenue","gross_profit","operating_inc","net_income",
                  "margen_bruto","margen_operativo","margen_neto"]
        tabla_margenes = _tabla_html(m, cols_m, "Resumen de Márgenes Históricos")

    # ── Transcripciones ─────────────────────────────────────────
    links_html = "".join(
        f'<li><a href="{url}" target="_blank">🔗 {nombre_link}</a></li>'
        for nombre_link, url in datos["transcripts_links"].items()
    )
    scanner_nav = (
        '<div class="nav-item" onclick="showSection(\'scanner\')">'
        "🌐 &nbsp;Global Event Scanner</div>"
        if scanner_url else ""
    )
    scanner_section = (
        f"""<section id="scanner" class="section scanner-section">
    <iframe title="Live global event-driven scanner" src="{scanner_url}" loading="lazy"
      referrerpolicy="no-referrer"></iframe>
  </section>"""
        if scanner_url else ""
    )
    idea_note = (
        "Use the Global Event Scanner section in this terminal to discover live filing and news leads. "
        "Verify each event and source before adding your own scenario assumptions below."
        if scanner_url else
        "Research workflow only — this dashboard does not scan live news, filings, or the market for catalysts. "
        "Add a candidate after verifying its event and source."
    )

    # ── Earnings sorpresa ───────────────────────────────────────
    earnings_rows = ""
    eh = datos.get("earnings_hist")
    if eh is not None and not eh.empty:
        for idx, row in eh.iterrows():
            fecha   = str(idx)[:10]
            eps_e   = row.get("EPS Estimate", "N/A")
            eps_r   = row.get("Reported EPS",  "N/A")
            sorpr   = row.get("Surprise(%)", None)
            cls     = "pos" if (sorpr or 0) > 0 else "neg"
            sorpr_s = f"{sorpr:.2f}%" if sorpr is not None else "N/A"
            try: eps_e = f"{float(eps_e):.2f}"
            except: pass
            try: eps_r = f"{float(eps_r):.2f}"
            except: pass
            earnings_rows += (
                f"<tr><td>{fecha}</td><td>{eps_e}</td>"
                f"<td>{eps_r}</td><td class='{cls}'>{sorpr_s}</td></tr>"
            )

    # ════════════════════════════════════════════════════════════
    #  HTML FINAL
    # ════════════════════════════════════════════════════════════
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Terminal Pro — {ticker}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
:root {{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2128;
  --border:#30363d; --accent:#58a6ff; --green:#3fb950;
  --red:#f85149; --yellow:#d29922; --text:#e6edf3;
  --muted:#8b949e; --purple:#bc8cff; --orange:#f0883e;
}}
*{{ box-sizing:border-box; margin:0; padding:0; }}
html,body{{ height:100%; background:var(--bg); color:var(--text);
  font-family:'IBM Plex Sans',sans-serif; font-size:14px; }}

/* ── SIDEBAR ── */
.layout{{ display:flex; height:100vh; overflow:hidden; }}
.sidebar{{
  width:220px; min-width:220px; background:var(--panel);
  border-right:1px solid var(--border);
  display:flex; flex-direction:column; overflow-y:auto;
}}
.sidebar-logo{{
  padding:20px 18px 14px;
  border-bottom:1px solid var(--border);
}}
.sidebar-logo h2{{ font-size:1rem; font-weight:700; color:var(--accent); }}
.sidebar-logo p{{ font-size:.72rem; color:var(--muted); margin-top:3px; }}
.nav-item{{
  padding:12px 18px; cursor:pointer; font-size:.82rem;
  color:var(--muted); border-left:3px solid transparent;
  transition:all .15s; display:flex; align-items:center; gap:10px;
}}
.nav-item:hover{{ background:var(--panel2); color:var(--text); }}
.nav-item.active{{ color:var(--accent); border-left-color:var(--accent);
  background:var(--panel2); font-weight:500; }}
.nav-section{{ padding:14px 18px 6px; font-size:.68rem;
  color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }}

/* ── MAIN ── */
.main{{ flex:1; overflow-y:auto; display:flex; flex-direction:column; }}
header{{
  padding:16px 28px; border-bottom:1px solid var(--border);
  background:var(--panel);
  display:flex; justify-content:space-between; align-items:flex-start;
}}
.header-left h1{{ font-size:1.25rem; font-weight:700; }}
.header-left p{{ font-size:.8rem; color:var(--muted); margin-top:3px; }}
.header-tags{{ display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }}
.tag{{
  font-size:.7rem; padding:3px 10px; border-radius:12px;
  border:1px solid var(--border); color:var(--muted);
}}

/* ── SECCIONES ── */
.section{{ display:none; padding:24px 28px; }}
.section.active{{ display:block; }}
.scanner-section.active{{ padding:0; height:100%; min-height:0; flex:1; }}
.scanner-section iframe{{ display:block; width:100%; height:calc(100vh - 12px); min-height:720px; border:0; }}

/* ── MÉTRICAS ── */
.metrics-grid{{
  display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  gap:10px; margin-bottom:22px;
}}
.metric-card{{
  background:var(--panel); border:1px solid var(--border);
  border-radius:8px; padding:12px 14px;
  display:flex; flex-direction:column; gap:3px;
}}
.m-label{{ font-size:.7rem; color:var(--muted); }}
.m-val{{
  font-size:1.05rem; font-weight:600;
  font-family:'IBM Plex Mono',monospace; color:var(--text);
}}
.m-sub{{ font-size:.7rem; color:var(--muted); }}

/* ── GRÁFICOS ── */
.chart-container{{
  background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:4px; margin-bottom:18px;
  overflow:hidden;
}}
.chart-row{{ display:grid; gap:16px; margin-bottom:16px; }}
.chart-row.cols-2{{ grid-template-columns:1fr 1fr; }}

/* ── TABLAS ── */
.table-wrap{{ margin-bottom:24px; }}
.table-title{{ font-size:.85rem; color:var(--accent); margin-bottom:8px; font-weight:500; }}
.table-scroll{{ overflow-x:auto; border:1px solid var(--border); border-radius:8px; }}
table{{ width:100%; border-collapse:collapse; font-size:.75rem; }}
thead tr{{ background:var(--panel2); }}
th{{
  padding:9px 12px; text-align:right;
  font-family:'IBM Plex Mono',monospace;
  color:var(--muted); font-weight:600;
  border-bottom:1px solid var(--border); white-space:nowrap;
}}
th:first-child{{ text-align:left; }}
td{{
  padding:7px 12px; text-align:right;
  border-bottom:1px solid var(--panel2);
  font-family:'IBM Plex Mono',monospace;
  white-space:nowrap;
}}
td:first-child{{ text-align:left; color:var(--muted); }}
tbody tr:hover{{ background:var(--panel2); }}
.pos{{ color:var(--green); }} .neg{{ color:var(--red); }}

/* ── TRANSCRIPCIONES ── */
.transcript-card{{
  background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:22px 24px; margin-bottom:16px;
}}
.transcript-card h3{{ color:var(--accent); margin-bottom:10px; }}
.transcript-card p{{ color:var(--muted); line-height:1.65; font-size:.85rem; }}
.transcript-card ul{{ padding-left:18px; margin-top:12px; }}
.transcript-card li{{ margin-bottom:9px; }}
.transcript-card a{{ color:var(--accent); text-decoration:none; }}
.transcript-card a:hover{{ text-decoration:underline; }}

/* ── LOADING PLACEHOLDER ── */
.no-data{{ color:var(--muted); font-style:italic; padding:20px; font-size:.85rem; }}

/* ── SECTION HEADING ── */
.sec-heading{{
  font-size:.78rem; font-weight:700; text-transform:uppercase;
  letter-spacing:.1em; color:var(--muted); margin:0 0 14px;
  padding-bottom:8px; border-bottom:1px solid var(--border);
}}

/* ── DESCRIPCION ── */
.desc-box{{
  background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:18px 22px; margin-bottom:18px;
  font-size:.82rem; line-height:1.7; color:var(--muted);
  max-height:120px; overflow:hidden; position:relative;
  cursor:pointer; transition:max-height .3s;
}}
.desc-box.expanded{{ max-height:600px; }}
.desc-box::after{{
  content:"▼ ver más"; position:absolute; bottom:10px; right:16px;
  font-size:.7rem; color:var(--accent);
}}
.desc-box.expanded::after{{ content:"▲ ver menos"; }}

/* ── IDEA GENERATOR ── */
.idea-note{{
  background:rgba(210,153,34,.1); border:1px solid var(--yellow);
  border-radius:8px; padding:12px 15px; color:var(--muted);
  font-size:.78rem; line-height:1.55; margin-bottom:18px;
}}
.idea-grid{{ display:grid; grid-template-columns:minmax(280px, .9fr) minmax(320px, 1.1fr); gap:16px; }}
.idea-panel{{
  background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:18px; margin-bottom:16px;
}}
.idea-panel h3{{ color:var(--accent); font-size:.9rem; margin-bottom:14px; }}
.idea-form{{ display:grid; grid-template-columns:1fr 1fr; gap:11px; }}
.idea-field{{ display:flex; flex-direction:column; gap:5px; }}
.idea-field.full{{ grid-column:1 / -1; }}
.idea-field label{{ color:var(--muted); font-size:.72rem; }}
.idea-field input,.idea-field select,.idea-field textarea{{
  width:100%; background:var(--bg); color:var(--text);
  border:1px solid var(--border); border-radius:6px; padding:9px;
  font:inherit; font-size:.78rem;
}}
.idea-field textarea{{ min-height:72px; resize:vertical; }}
.idea-button{{
  background:var(--accent); color:#0d1117; border:0; border-radius:6px;
  padding:10px 14px; font-weight:700; cursor:pointer;
}}
.idea-button:hover{{ filter:brightness(1.1); }}
.idea-checklist{{ color:var(--muted); font-size:.78rem; line-height:1.6; }}
.idea-checklist strong{{ color:var(--text); }}
.idea-list{{ display:grid; gap:10px; }}
.idea-card{{
  background:var(--panel); border:1px solid var(--border);
  border-radius:9px; padding:14px 16px;
}}
.idea-card-head{{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.idea-card h4{{ font-size:.9rem; }}
.idea-card p{{ color:var(--muted); font-size:.76rem; line-height:1.55; margin-top:7px; }}
.idea-stats{{ display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }}
.idea-stat{{ background:var(--panel2); border-radius:5px; padding:5px 8px; font-size:.7rem; color:var(--muted); }}
.idea-stat strong{{ color:var(--text); }}
.idea-delete{{ background:none; border:1px solid var(--border); color:var(--muted); border-radius:5px; padding:5px 8px; cursor:pointer; }}
.idea-delete:hover{{ color:var(--red); border-color:var(--red); }}
.idea-card a{{ color:var(--accent); font-size:.75rem; }}
.idea-error{{ color:var(--red); font-size:.78rem; margin-top:8px; }}
@media(max-width:850px){{ .idea-grid{{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="layout">

<!-- ═══════════ SIDEBAR ═══════════ -->
<nav class="sidebar">
  <div class="sidebar-logo">
    <h2>📊 {ticker}</h2>
    <p>{nombre[:32]}</p>
  </div>
  <div class="nav-section">Análisis</div>
  <div class="nav-item active" onclick="showSection('dashboard')">
    🏠 &nbsp;Dashboard
  </div>
  <div class="nav-item" onclick="showSection('ideas')">
    💡 &nbsp;Idea Generator
  </div>
  {scanner_nav}
  <div class="nav-section">Estados Financieros</div>
  <div class="nav-item" onclick="showSection('income')">
    📋 &nbsp;Income Statement
  </div>
  <div class="nav-item" onclick="showSection('balance')">
    🏦 &nbsp;Balance Sheet
  </div>
  <div class="nav-item" onclick="showSection('cashflow')">
    💧 &nbsp;Cash Flow
  </div>
  <div class="nav-section">Valoración</div>
  <div class="nav-item" onclick="showSection('ratios')">
    🎯 &nbsp;Ratios
  </div>
  <div class="nav-item" onclick="showSection('earnings')">
    📅 &nbsp;Earnings Hist.
  </div>
  <div class="nav-section">Recursos</div>
  <div class="nav-item" onclick="showSection('transcripciones')">
    🎙️ &nbsp;Transcripciones
  </div>
</nav>

<!-- ═══════════ MAIN ═══════════ -->
<div class="main">

  <!-- Header -->
  <header>
    <div class="header-left">
      <h1>{nombre} ({ticker})</h1>
      <p>{intercambio}</p>
      <div class="header-tags">
        <span class="tag">{sector}</span>
        <span class="tag">{industria}</span>
      </div>
    </div>
  </header>

  <!-- ══ SECCIÓN: DASHBOARD ══ -->
  <section id="dashboard" class="section active">
    <p class="sec-heading">Resumen General</p>
    <div class="metrics-grid">{header_cards}</div>
    <div class="desc-box" onclick="this.classList.toggle('expanded')">{descripcion}</div>
    <div class="chart-container"><div id="c_precio"></div></div>
  </section>

  <!-- ══ SECCIÓN: EVENT-DRIVEN IDEAS ══ -->
  <section id="ideas" class="section">
    <p class="sec-heading">Event-Driven Special Situations Idea Generator</p>
    <div class="idea-note">
      {idea_note} Your ideas are stored in this browser on this device.
      Scenario values and probabilities are your own assumptions, not forecasts or investment advice.
    </div>
    <div class="idea-grid">
      <div>
        <div class="idea-panel">
          <h3>Event-pattern playbook</h3>
          <div class="idea-field">
            <label for="ideaPattern">Situation type</label>
            <select id="ideaPattern"></select>
          </div>
          <div id="ideaChecklist" class="idea-checklist" style="margin-top:12px"></div>
        </div>
        <div class="idea-panel">
          <h3>Add a researched candidate</h3>
          <form id="ideaForm" class="idea-form">
            <div class="idea-field">
              <label for="ideaTicker">Ticker</label>
              <input id="ideaTicker" name="ticker" required maxlength="20">
            </div>
            <div class="idea-field">
              <label for="ideaCurrent">Current price ($)</label>
              <input id="ideaCurrent" name="current" type="number" min="0.01" step="any" required>
            </div>
            <div class="idea-field">
              <label for="ideaDate">Catalyst date</label>
              <input id="ideaDate" name="date" type="date" required>
            </div>
            <div class="idea-field">
              <label for="ideaProbability">Your base-case probability (%)</label>
              <input id="ideaProbability" name="probability" type="number" min="0" max="100" step="1" value="50" required>
            </div>
            <div class="idea-field">
              <label for="ideaBase">Base-case value ($)</label>
              <input id="ideaBase" name="base" type="number" min="0.01" step="any" required>
            </div>
            <div class="idea-field">
              <label for="ideaDownside">Downside-case value ($)</label>
              <input id="ideaDownside" name="downside" type="number" min="0.01" step="any" required>
            </div>
            <div class="idea-field full">
              <label for="ideaCatalyst">Catalyst and thesis</label>
              <textarea id="ideaCatalyst" name="catalyst" maxlength="800" required placeholder="What is the event, why might the market be mispricing it, and what would invalidate the thesis?"></textarea>
            </div>
            <div class="idea-field">
              <label for="ideaSourceName">Evidence / source name</label>
              <input id="ideaSourceName" name="sourceName" maxlength="100" required placeholder="e.g. Merger agreement, 8-K">
            </div>
            <div class="idea-field">
              <label for="ideaSourceUrl">Primary-source URL</label>
              <input id="ideaSourceUrl" name="sourceUrl" type="url" required placeholder="https://...">
            </div>
            <div class="idea-field full">
              <button class="idea-button" type="submit">Add candidate to ranked watchlist</button>
              <div id="ideaError" class="idea-error" role="alert"></div>
            </div>
          </form>
        </div>
      </div>
      <div class="idea-panel">
        <h3>Ranked research watchlist</h3>
        <p class="idea-checklist" style="margin-bottom:12px">
          Rank combines your probability-weighted scenario return, downside buffer, catalyst date, and evidence completeness.
          It is a research-priority score, not a recommendation.
        </p>
        <div id="ideaList" class="idea-list" aria-live="polite"></div>
      </div>
    </div>
  </section>

  {scanner_section}

  <!-- ══ SECCIÓN: INCOME STATEMENT ══ -->
  <section id="income" class="section">
    <p class="sec-heading">Estado de Resultados — Análisis Histórico</p>
    <div class="chart-row cols-2">
      <div class="chart-container"><div id="c_rev"></div></div>
      <div class="chart-container"><div id="c_gastos"></div></div>
    </div>
    <div class="chart-container"><div id="c_yoy"></div></div>
    {tabla_margenes}
  </section>

  <!-- ══ SECCIÓN: BALANCE SHEET ══ -->
  <section id="balance" class="section">
    <p class="sec-heading">Balance General — Composición y Estructura de Capital</p>
    <div class="chart-row cols-2">
      <div class="chart-container"><div id="c_activos"></div></div>
      <div class="chart-container"><div id="c_capital"></div></div>
    </div>
  </section>

  <!-- ══ SECCIÓN: CASH FLOW ══ -->
  <section id="cashflow" class="section">
    <p class="sec-heading">Flujo de Caja — Cascada y Serie Histórica</p>
    <div class="chart-row cols-2">
      <div class="chart-container"><div id="c_wfall"></div></div>
      <div class="chart-container"><div id="c_cfserie"></div></div>
    </div>
  </section>

  <!-- ══ SECCIÓN: RATIOS ══ -->
  <section id="ratios" class="section">
    <p class="sec-heading">Ratios de Valoración</p>
    <div class="chart-container"><div id="c_ratios"></div></div>
    <p class="no-data" style="font-size:.75rem; margin-top:8px;">
      ⚠️ Los gauges muestran benchmarks típicos del mercado como referencia. Cada sector tiene rangos distintos.
    </p>
  </section>

  <!-- ══ SECCIÓN: EARNINGS ══ -->
  <section id="earnings" class="section">
    <p class="sec-heading">Historial de Earnings por Trimestre</p>
    <div class="table-wrap">
      <p class="table-title">📅 EPS Estimado vs Reportado</p>
      <div class="table-scroll">
        <table>
          <thead><tr>
            <th>Fecha</th><th>EPS Estimado</th>
            <th>EPS Reportado</th><th>Sorpresa %</th>
          </tr></thead>
          <tbody>{earnings_rows or '<tr><td colspan="4" class="no-data">Sin datos de earnings</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- ══ SECCIÓN: TRANSCRIPCIONES ══ -->
  <section id="transcripciones" class="section">
    <p class="sec-heading">Transcripciones de Earnings Calls</p>
    <div class="transcript-card">
      <h3>🎙️ ¿Dónde encontrar las transcripciones?</h3>
      <p>
        Las APIs gratuitas no distribuyen transcripciones completas de earnings calls.
        Estas son las fuentes más confiables para acceder al texto completo:
      </p>
      <ul>{links_html}</ul>
    </div>
    <div class="transcript-card">
      <h3>📄 Descripción Corporativa Completa</h3>
      <p>{descripcion}</p>
    </div>
  </section>

</div><!-- /main -->
</div><!-- /layout -->

<script>
// ── Datos de gráficos ──────────────────────────────────────
const FIGS = {{
  precio:  {fig_precio_js},
  ratios:  {fig_ratios_js},
  rev:     {fig_rev_js},
  gastos:  {fig_gastos_js},
  yoy:     {fig_yoy_js},
  activos: {fig_activos_js},
  capital: {fig_cap_js},
  wfall:   {fig_wfall_js},
  cfserie: {fig_cfserie_js},
}};

const IDEA_DEFAULTS = {idea_defaults_json};
const IDEA_STORAGE_KEY = 'value-investing-terminal-ideas-v1';
const IDEA_PATTERNS = {{
  spinoff: {{
    label: 'Spin-off / forced selling',
    trigger: 'A recently separated business may be sold by holders that cannot or do not want to own the new company.',
    evidence: 'Separation filing, Form 10, distribution date, pro forma financials, and standalone guidance.',
    risk: 'Stranded costs, parent dependencies, leverage, weak standalone economics, or selling already reflected in price.'
  }},
  merger: {{
    label: 'Merger arbitrage',
    trigger: 'The target trades below announced deal consideration while the transaction is pending.',
    evidence: 'Merger agreement, consideration and exchange terms, regulatory filings, financing commitments, and closing conditions.',
    risk: 'Regulatory challenge, financing failure, vote risk, delay, or a material gap to standalone value if the deal breaks.'
  }},
  busted: {{
    label: 'Busted-deal recovery',
    trigger: 'After a transaction ends, selling or deal overhang may leave the standalone business mispriced.',
    evidence: 'Termination notice, break-fee terms, updated guidance, debt maturities, and post-deal liquidity.',
    risk: 'The original deal price may not represent standalone value; the deal may have exposed deeper operating problems.'
  }},
  restructuring: {{
    label: 'Post-restructuring equity',
    trigger: 'New securities after bankruptcy or recapitalization may be underfollowed and hard to value.',
    evidence: 'Plan of reorganization, emergence filings, new cap table, exit financing, warrants, and operating plan.',
    risk: 'Dilution, liquidity needs, execution challenges, or a capital structure that leaves little equity value.'
  }},
  rights: {{
    label: 'Rights offering / recapitalization',
    trigger: 'Complex funding terms or dilution may create a temporary discount—or signal distress.',
    evidence: 'Offering circular, subscription/backstop terms, pro forma share count, debt terms, and use of proceeds.',
    risk: 'Insufficient participation, worse-than-expected dilution, or proceeds failing to address solvency.'
  }},
  activist: {{
    label: 'Activist / strategic review',
    trigger: 'Governance pressure or a strategic review could prompt asset sales, a separation, or a change in capital allocation.',
    evidence: 'Activist filings, company response, board changes, strategic-review announcement, and timeline.',
    risk: 'No control over outcome, stakeholder resistance, execution delay, or a catalyst that never materializes.'
  }},
  tender: {{
    label: 'Tender offer / buyback dislocation',
    trigger: 'Tender mechanics or a repurchase may cause a temporary pricing mismatch.',
    evidence: 'Offer documents, price and proration terms, eligibility, funding, expiration date, and tax treatment.',
    risk: 'Proration, changing terms, insufficient funding, or mistaking a capital return for durable value creation.'
  }},
  index: {{
    label: 'Index / mandate-driven selling',
    trigger: 'A rules-based removal or mandate change may create selling unrelated to near-term fundamentals.',
    evidence: 'Index provider notice, effective date, affected funds, liquidity, and evidence of underlying business value.',
    risk: 'Flow estimates may be wrong, the event may be priced in, or the removal may reflect genuine deterioration.'
  }}
}};

const ID_MAP = {{
  'dashboard':      [['c_precio', 'precio']],
  'income':         [['c_rev','rev'],['c_gastos','gastos'],['c_yoy','yoy']],
  'balance':        [['c_activos','activos'],['c_capital','capital']],
  'cashflow':       [['c_wfall','wfall'],['c_cfserie','cfserie']],
  'ratios':         [['c_ratios','ratios']],
  'earnings':       [],
  'ideas':          [],
  'transcripciones':[],
}};

const rendered = {{}};   // caché para no re-renderizar

function showSection(id) {{
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.currentTarget.classList.add('active');

  // Renderizar gráficas lazy (solo al hacer clic en la pestaña)
  (ID_MAP[id] || []).forEach(([divId, figKey]) => {{
    if (!rendered[divId] && FIGS[figKey]) {{
      Plotly.newPlot(divId, FIGS[figKey].data, FIGS[figKey].layout, {{responsive:true}});
      rendered[divId] = true;
    }}
  }});
}}

const ideaPattern = document.getElementById('ideaPattern');
const ideaForm = document.getElementById('ideaForm');
const ideaList = document.getElementById('ideaList');
const ideaError = document.getElementById('ideaError');
let ideas = [];

function escapeIdeaText(value) {{
  return String(value).replace(/[&<>"']/g, char => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }})[char]);
}}

function validSourceUrl(value) {{
  try {{
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : '';
  }} catch (error) {{
    return '';
  }}
}}

function showPatternChecklist() {{
  const pattern = IDEA_PATTERNS[ideaPattern.value];
  document.getElementById('ideaChecklist').innerHTML =
    '<p><strong>Screen for:</strong> ' + escapeIdeaText(pattern.trigger) + '</p>' +
    '<p><strong>Verify:</strong> ' + escapeIdeaText(pattern.evidence) + '</p>' +
    '<p><strong>Failure modes:</strong> ' + escapeIdeaText(pattern.risk) + '</p>';
}}

function ideaMetrics(idea) {{
  const upside = (idea.base / idea.current - 1) * 100;
  const downside = (1 - idea.downside / idea.current) * 100;
  const riskReward = downside > 0 ? upside / downside : 0;
  const expected = (idea.probability / 100) * upside -
    (1 - idea.probability / 100) * downside;
  const expectedScore = Math.max(0, Math.min(100, (expected + 30) / 60 * 100));
  const downsideScore = Math.max(0, Math.min(100, 100 - downside * 3.33));
  const dateIsFuture = new Date(idea.date + 'T23:59:59') >= new Date();
  const score = Math.round(expectedScore * .5 + downsideScore * .25 +
    (validSourceUrl(idea.sourceUrl) ? 5 : 0) +
    (dateIsFuture ? 10 : 0) + (idea.catalyst ? 10 : 0));
  return {{upside, downside, riskReward, expected, score, dateIsFuture}};
}}

function renderIdeas() {{
  ideas.sort((a, b) => ideaMetrics(b).score - ideaMetrics(a).score);
  if (!ideas.length) {{
    ideaList.innerHTML = '<p class="no-data">No candidates yet. Add a sourced event thesis to start your watchlist.</p>';
    return;
  }}
  ideaList.innerHTML = ideas.map(idea => {{
    const metrics = ideaMetrics(idea);
    const patternName = IDEA_PATTERNS[idea.pattern].label;
    const safeUrl = validSourceUrl(idea.sourceUrl);
    const source = safeUrl
      ? '<a href="' + escapeIdeaText(safeUrl) + '" target="_blank" rel="noopener noreferrer">' +
        escapeIdeaText(idea.sourceName) + ' ↗</a>'
      : escapeIdeaText(idea.sourceName);
    return '<article class="idea-card">' +
      '<div class="idea-card-head"><div><h4>' + escapeIdeaText(idea.ticker) +
      ' · ' + escapeIdeaText(patternName) + '</h4><p>Catalyst: ' +
      escapeIdeaText(idea.date) + (metrics.dateIsFuture ? '' : ' · date passed; re-verify') +
      '</p></div><div><span class="idea-stat"><strong>' + metrics.score +
      '</strong> / 100 rank</span> <button class="idea-delete" type="button" data-delete="' +
      escapeIdeaText(idea.id) + '" aria-label="Remove ' + escapeIdeaText(idea.ticker) +
      '">Remove</button></div></div><p>' + escapeIdeaText(idea.catalyst) + '</p>' +
      '<div class="idea-stats"><span class="idea-stat">Expected scenario return: <strong>' +
      metrics.expected.toFixed(1) + '%</strong></span><span class="idea-stat">Base-case upside: <strong>' +
      metrics.upside.toFixed(1) + '%</strong></span><span class="idea-stat">Downside case: <strong>' +
      metrics.downside.toFixed(1) + '%</strong></span><span class="idea-stat">Upside / downside: <strong>' +
      metrics.riskReward.toFixed(2) + 'x</strong></span><span class="idea-stat">Base-case probability: <strong>' +
      idea.probability + '%</strong></span></div><p>Evidence: ' + source + '</p></article>';
  }}).join('');
}}

function saveIdeas() {{
  try {{
    localStorage.setItem(IDEA_STORAGE_KEY, JSON.stringify(ideas));
    ideaError.textContent = '';
  }} catch (error) {{
    ideaError.textContent = 'Could not save this watchlist in browser storage: ' + error.message;
  }}
}}

Object.entries(IDEA_PATTERNS).forEach(([key, pattern]) => {{
  const option = document.createElement('option');
  option.value = key;
  option.textContent = pattern.label;
  ideaPattern.appendChild(option);
}});
document.getElementById('ideaTicker').value = IDEA_DEFAULTS.ticker;
document.getElementById('ideaCurrent').value = IDEA_DEFAULTS.price || '';
ideaPattern.addEventListener('change', showPatternChecklist);
showPatternChecklist();

try {{
  const savedIdeas = localStorage.getItem(IDEA_STORAGE_KEY);
  if (savedIdeas) {{
    const parsedIdeas = JSON.parse(savedIdeas);
    if (!Array.isArray(parsedIdeas) || parsedIdeas.some(idea =>
        !idea || !IDEA_PATTERNS[idea.pattern] || !idea.id ||
        !Number.isFinite(idea.current) || !Number.isFinite(idea.base) ||
        !Number.isFinite(idea.downside) || idea.downside >= idea.current ||
        idea.base <= idea.downside || !Number.isFinite(idea.probability) ||
        !idea.date || !idea.sourceName || !idea.catalyst)) {{
      throw new Error('Stored watchlist contains invalid idea data.');
    }}
    ideas = parsedIdeas;
  }}
}} catch (error) {{
  ideaError.textContent = 'Could not load the saved watchlist: ' + error.message;
}}
renderIdeas();

ideaForm.addEventListener('submit', formEvent => {{
  formEvent.preventDefault();
  ideaError.textContent = '';
  const values = new FormData(ideaForm);
  const tickerValue = String(values.get('ticker')).trim().toUpperCase();
  const current = Number(values.get('current'));
  const base = Number(values.get('base'));
  const downside = Number(values.get('downside'));
  const probability = Number(values.get('probability'));
  const sourceUrl = validSourceUrl(String(values.get('sourceUrl')).trim());
  if (!sourceUrl) {{
    ideaError.textContent = 'Enter a valid HTTP or HTTPS source URL.';
    return;
  }}
  if (!tickerValue || !Number.isFinite(current) || current <= 0 ||
      !Number.isFinite(base) || base <= 0 || !Number.isFinite(downside) ||
      downside <= 0 || downside >= current || base <= downside ||
      !Number.isFinite(probability) ||
      probability < 0 || probability > 100) {{
    ideaError.textContent = 'Enter a ticker, positive prices with downside below current and base case above downside, and probability from 0 to 100.';
    return;
  }}
  ideas.push({{
    id: Date.now().toString(36) + Math.random().toString(36).slice(2),
    ticker: tickerValue,
    pattern: ideaPattern.value,
    current,
    base,
    downside,
    probability,
    date: String(values.get('date')),
    catalyst: String(values.get('catalyst')).trim(),
    sourceName: String(values.get('sourceName')).trim(),
    sourceUrl
  }});
  saveIdeas();
  renderIdeas();
  ideaForm.reset();
  document.getElementById('ideaTicker').value = IDEA_DEFAULTS.ticker;
  document.getElementById('ideaCurrent').value = IDEA_DEFAULTS.price || '';
  document.getElementById('ideaProbability').value = 50;
}});

ideaList.addEventListener('click', clickEvent => {{
  const button = clickEvent.target.closest('[data-delete]');
  if (!button) return;
  ideas = ideas.filter(idea => idea.id !== button.dataset.delete);
  saveIdeas();
  renderIdeas();
}});

// Renderizar sección inicial
(ID_MAP['dashboard'] || []).forEach(([divId, figKey]) => {{
  if (FIGS[figKey]) {{
    Plotly.newPlot(divId, FIGS[figKey].data, FIGS[figKey].layout, {{responsive:true}});
    rendered[divId] = true;
  }}
}});
</script>
</body>
</html>"""
    return html


# ──────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────

def _fmt(n) -> str:
    """Formatea número grande con sufijo K/M/B/T."""
    try:
        n = float(n)
        if n >= 1e12: return f"${n/1e12:.2f}T"
        if n >= 1e9:  return f"${n/1e9:.2f}B"
        if n >= 1e6:  return f"${n/1e6:.2f}M"
        if n >= 1e3:  return f"${n/1e3:.2f}K"
        return f"${n:.2f}"
    except Exception:
        return "N/A"


def abrir_chrome(url: str):
    """Intenta abrir en Chrome; si no, usa el navegador por defecto."""
    rutas_chrome = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe %s",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe %s",
        r"/usr/bin/google-chrome %s",
        r"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome %s",
    ]
    for ruta in rutas_chrome:
        exe = ruta.replace(" %s", "")
        if os.path.exists(exe):
            try:
                webbrowser.get(ruta).open(url)
                return
            except Exception:
                continue
    webbrowser.open(url)


def _start_embedded_scanner() -> str:
    dashboard_port = int(os.environ.get("IBKR_DASHBOARD_PORT", "8765"))
    if not 1 <= dashboard_port <= 65535:
        raise ValueError("IBKR_DASHBOARD_PORT must be between 1 and 65535.")
    scanner_url = f"http://127.0.0.1:{dashboard_port}/"
    try:
        response = requests.get(scanner_url, timeout=1)
        if response.status_code == 200 and "Global event-driven market scan" in response.text:
            return scanner_url
    except requests.RequestException:
        pass

    try:
        from ibkr_market_scanner import run_live_dashboard
    except ImportError as error:
        raise RuntimeError(
            f"Install the IBKR API package to start the integrated event scanner: {error}"
        ) from error

    startup_errors = []

    def serve_dashboard():
        try:
            run_live_dashboard(open_browser=False)
        except Exception as error:
            startup_errors.append(error)

    scanner_thread = threading.Thread(
        target=serve_dashboard,
        name="integrated-event-scanner",
        daemon=False,
    )
    scanner_thread.start()
    for _ in range(100):
        if startup_errors:
            raise RuntimeError(f"Could not start the integrated event scanner: {startup_errors[0]}")
        if not scanner_thread.is_alive():
            raise RuntimeError("The integrated event scanner stopped during startup.")
        try:
            response = requests.get(scanner_url, timeout=1)
            if response.status_code == 200 and "Global event-driven market scan" in response.text:
                return scanner_url
        except requests.RequestException:
            time.sleep(0.1)
    raise RuntimeError(
        f"The integrated event scanner did not respond at {scanner_url}."
    )


# ──────────────────────────────────────────────────────────────────
#  PUNTO DE ENTRADA
# ──────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 58)
    print("  📊  TERMINAL FINANCIERA PRO — Dashboard Institucional")
    print("  Fuente: Alpha Vantage (10 años) + yfinance")
    print("═" * 58)

    workflow = input(
        "\n  [1] Terminal completo (análisis de empresa + escáner global integrado)\n"
        "  [2] Escáner IBKR independiente con dashboard y refresh manual\n"
        "  [3] Escaneo único IBKR y exportación\n"
        "  Elige (default 1): "
    ).strip() or "1"
    if workflow == "2":
        try:
            from ibkr_market_scanner import run_live_dashboard
        except ImportError as error:
            print(f"\n  ❌ Install the IBKR API package first: pip install ib_async ({error})")
            return

        try:
            run_live_dashboard()
        except (RuntimeError, ValueError, OSError) as error:
            print(f"\n  ❌ {error}")
            return
        return
    if workflow == "3":
        try:
            from ibkr_market_scanner import run_market_scan
        except ImportError as error:
            print(f"\n  ❌ Install the IBKR API package first: pip install ib_async ({error})")
            return
        try:
            report_path = run_market_scan()
        except (RuntimeError, ValueError) as error:
            print(f"\n  ❌ {error}")
            return
        print(f"\n  ✅ Reporte de ideas guardado: {report_path.name}")
        print(f"  📂 Ruta: {report_path}")
        abrir_chrome(report_path.as_uri())
        return
    if workflow != "1":
        print("  ❌ Opción inválida. Elige 1, 2 o 3.")
        return

    ticker = input("\n  Introduce el Ticker (ej. NVDA, AAPL, MSFT): ").upper().strip()
    if not ticker:
        print("  ❌ Ticker vacío. Saliendo.")
        return

    periodos = {"1": "6mo", "2": "1y", "3": "2y", "4": "5y"}
    print("\n  Período del gráfico de precios:")
    print("  [1] 6 meses  [2] 1 año  [3] 2 años  [4] 5 años")
    op = input("  Elige (default 3 → 2 años): ").strip() or "3"
    periodo = periodos.get(op, "2y")

    # Obtener datos
    datos = obtener_todos_los_datos(ticker, periodo)

    if datos["hist"].empty:
        print(f"\n  ❌ No se encontraron precios para '{ticker}'. Verifica el ticker.")
        return

    # Construir y guardar HTML
    print("  ⚙️  Construyendo dashboard HTML…")
    scanner_url = ""
    try:
        scanner_url = _start_embedded_scanner()
    except (RuntimeError, ValueError, OSError) as error:
        print(f"\n  ⚠️  El escáner global no pudo integrarse: {error}")
        print("  El análisis de empresa continuará sin la pestaña del escáner.")
    html = construir_html(datos, scanner_url=scanner_url)

    nombre_archivo = f"terminal_pro_{ticker}.html"
    with open(nombre_archivo, "w", encoding="utf-8") as fh:
        fh.write(html)

    ruta = os.path.abspath(nombre_archivo)
    print(f"\n  ✅ Reporte guardado: {nombre_archivo}")
    print(f"  📂 Ruta: {ruta}")
    print("\n  Secciones disponibles en el sidebar:")
    print("  🏠 Dashboard  💡 Idea Generator  🌐 Global Event Scanner")
    print("  📋 Income Statement")
    print("  🏦 Balance Sheet  💧 Cash Flow  🎯 Ratios")
    print("  📅 Earnings  🎙️ Transcripciones")

    abrir_chrome(f"file:///{ruta}")


if __name__ == "__main__":
    main()

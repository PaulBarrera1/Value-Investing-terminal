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

AV_KEY = "Paulbarrera1"          # Alpha Vantage API Key
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


def construir_html(datos: dict) -> str:
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

const ID_MAP = {{
  'dashboard':      [['c_precio', 'precio']],
  'income':         [['c_rev','rev'],['c_gastos','gastos'],['c_yoy','yoy']],
  'balance':        [['c_activos','activos'],['c_capital','capital']],
  'cashflow':       [['c_wfall','wfall'],['c_cfserie','cfserie']],
  'ratios':         [['c_ratios','ratios']],
  'earnings':       [],
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


# ──────────────────────────────────────────────────────────────────
#  PUNTO DE ENTRADA
# ──────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 58)
    print("  📊  TERMINAL FINANCIERA PRO — Dashboard Institucional")
    print("  Fuente: Alpha Vantage (10 años) + yfinance")
    print("═" * 58)

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
    html = construir_html(datos)

    nombre_archivo = f"terminal_pro_{ticker}.html"
    with open(nombre_archivo, "w", encoding="utf-8") as fh:
        fh.write(html)

    ruta = os.path.abspath(nombre_archivo)
    print(f"\n  ✅ Reporte guardado: {nombre_archivo}")
    print(f"  📂 Ruta: {ruta}")
    print("\n  Secciones disponibles en el sidebar:")
    print("  🏠 Dashboard  📋 Income Statement  🏦 Balance Sheet")
    print("  💧 Cash Flow  🎯 Ratios  📅 Earnings  🎙️ Transcripciones")

    abrir_chrome(f"file:///{ruta}")


if __name__ == "__main__":
    main()

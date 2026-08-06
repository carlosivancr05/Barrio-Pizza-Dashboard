"""
data_processing.py
-------------------
Toda la lógica de negocio de Barrio Pizza, separada del dashboard (app.py)
para que sea fácil de leer, testear y explicar en el video.

Flujo:
1. cargar datos (4 CSV)
2. proyectar consumo de la próxima semana por sucursal + ingrediente
3. calcular necesidad real = proyección - stock actual
4. convertir la orden de compra (formatos) a unidad base
5. comparar orden vs necesidad real -> generar alertas
6. extras: detectar ingredientes olvidados, ingredientes no catalogados,
   pedidos "raros" comparando sucursales entre sí, y agrupar por proveedor.
"""

import unicodedata

import numpy as np
import pandas as pd

SEMANAS_COLS = ["S1", "S2", "S3", "S4", "S5", "S6"]


# ---------------------------------------------------------------------------
# 1. CARGA DE DATOS
# ---------------------------------------------------------------------------
def cargar_datos(ingredientes_df, consumo_df, inventario_df, orden_df):
    """Normaliza tipos y nombres para que el resto del pipeline no se rompa
    por espacios, mayúsculas o formatos raros."""
    ingredientes_df = ingredientes_df.copy()
    consumo_df = consumo_df.copy()
    inventario_df = inventario_df.copy()
    orden_df = orden_df.copy()

    for df in (ingredientes_df, consumo_df, inventario_df, orden_df):
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].astype(str).str.strip()

    consumo_df["consumo_unidad_base"] = pd.to_numeric(
        consumo_df["consumo_unidad_base"], errors="coerce"
    )
    inventario_df["stock_actual_unidad_base"] = pd.to_numeric(
        inventario_df["stock_actual_unidad_base"], errors="coerce"
    )
    orden_df["cantidad_formatos"] = pd.to_numeric(
        orden_df["cantidad_formatos"], errors="coerce"
    )
    ingredientes_df["unidad_base_por_formato"] = pd.to_numeric(
        ingredientes_df["unidad_base_por_formato"], errors="coerce"
    )
    if "precio_referencia_usd" in ingredientes_df.columns:
        ingredientes_df["precio_referencia_usd"] = pd.to_numeric(
            ingredientes_df["precio_referencia_usd"], errors="coerce"
        ).fillna(0.0)
    else:
        # Si el catálogo no trae precios (ej. el usuario subió su propio
        # ingredientes.csv sin esa columna), el impacto en $ simplemente da 0
        # en vez de romper el resto del dashboard.
        ingredientes_df["precio_referencia_usd"] = 0.0

    return ingredientes_df, consumo_df, inventario_df, orden_df


# ---------------------------------------------------------------------------
# 2. PROYECCIÓN DE CONSUMO (más inteligente que un promedio simple)
# ---------------------------------------------------------------------------
def _proyectar_serie(valores):
    """
    Recibe las 6 semanas de consumo (S1..S6) de UN ingrediente en UNA sucursal
    y devuelve la proyección para la semana 7, junto con una nota de qué método usó.

    Método:
    - Si faltan datos (NaN) los ignora.
    - Detecta semanas atípicas con MAD (Median Absolute Deviation), una medida
      robusta de outliers que no se deja engañar por 1 semana rara (ej. un evento,
      un error de registro).
    - Ajusta una recta de tendencia (regresión lineal) sobre las semanas restantes
      para capturar si el consumo está subiendo o bajando, no solo su promedio.
    - Acota (clip) la proyección para que una tendencia de solo 6 puntos no
      extrapole a algo absurdo (ni por debajo de 0, ni muy por encima del máximo
      histórico).
    """
    valores = np.array(valores, dtype=float)
    semanas = np.arange(1, len(valores) + 1)

    mask_validos = ~np.isnan(valores)
    v = valores[mask_validos]
    s = semanas[mask_validos]

    if len(v) == 0:
        return 0.0, "sin_datos"
    if len(v) == 1:
        return float(v[0]), "un_solo_dato"

    # --- detección de outliers robusta (MAD) ---
    mediana = np.median(v)
    mad = np.median(np.abs(v - mediana)) or 1e-9
    z_robusto = 0.6745 * (v - mediana) / mad
    no_outlier = np.abs(z_robusto) <= 3.0  # umbral típico para MAD

    v_limpio = v[no_outlier]
    s_limpio = s[no_outlier]
    hubo_outlier = (~no_outlier).any()

    if len(v_limpio) < 3:
        # Muy pocos puntos confiables -> mejor promedio simple que una recta
        return float(np.mean(v)), "promedio_simple"

    # --- regresión lineal (tendencia) sobre los puntos limpios ---
    pendiente, intercepto = np.polyfit(s_limpio, v_limpio, 1)
    proyeccion = pendiente * (semanas.max() + 1) + intercepto

    # --- guardarraíles: no permitir extrapolaciones absurdas ---
    piso = max(0.0, 0.5 * v.min())
    techo = 1.75 * v.max()
    proyeccion_acotada = float(np.clip(proyeccion, piso, techo))

    metodo = "tendencia_lineal" + ("_sin_outliers" if hubo_outlier else "")
    return proyeccion_acotada, metodo


def proyectar_consumo(consumo_df):
    """Pivotea el histórico a formato ancho (S1..S6) y proyecta semana 7
    para cada combinación sucursal + ingrediente."""
    pivot = consumo_df.pivot_table(
        index=["sucursal", "ingrediente_id"],
        columns="semana",
        values="consumo_unidad_base",
        aggfunc="mean",
    ).reindex(columns=SEMANAS_COLS)

    resultados = []
    for (sucursal, ingrediente_id), fila in pivot.iterrows():
        proyeccion, metodo = _proyectar_serie(fila.values)
        registro = {
            "sucursal": sucursal,
            "ingrediente_id": ingrediente_id,
            "consumo_promedio_6sem": float(np.nanmean(fila.values)),
            "proyeccion_prox_semana": proyeccion,
            "metodo_proyeccion": metodo,
        }
        # Guardamos también las 6 semanas crudas (S1..S6) para poder graficar
        # "por qué" se llegó a esa proyección (transparencia del cálculo).
        for semana in SEMANAS_COLS:
            registro[semana] = float(fila[semana]) if not pd.isna(fila[semana]) else None
        resultados.append(registro)
    return pd.DataFrame(resultados)


# ---------------------------------------------------------------------------
# 3-5. NECESIDAD REAL, CONVERSIÓN DE UNIDADES Y ALERTAS
# ---------------------------------------------------------------------------
def construir_tabla_maestra(ingredientes_df, consumo_df, inventario_df, orden_df):
    """Une todo en una sola tabla por (sucursal, ingrediente) y calcula
    la necesidad real, la orden convertida a unidad base, y el veredicto."""

    proyeccion_df = proyectar_consumo(consumo_df)

    catalogo_ids = set(ingredientes_df["ingrediente_id"])

    # Ingredientes en la orden que NO existen en el catálogo -> problema de datos
    ids_orden = set(orden_df["ingrediente_id"])
    ids_desconocidos = ids_orden - catalogo_ids
    orden_desconocida = orden_df[orden_df["ingrediente_id"].isin(ids_desconocidos)].copy()
    orden_valida = orden_df[~orden_df["ingrediente_id"].isin(ids_desconocidos)].copy()

    # Universo completo: cada sucursal x cada ingrediente del catálogo
    sucursales = sorted(
        set(consumo_df["sucursal"]) | set(inventario_df["sucursal"]) | set(orden_df["sucursal"])
    )
    universo = pd.MultiIndex.from_product(
        [sucursales, sorted(catalogo_ids)], names=["sucursal", "ingrediente_id"]
    ).to_frame(index=False)

    df = universo.merge(proyeccion_df, on=["sucursal", "ingrediente_id"], how="left")
    df = df.merge(inventario_df, on=["sucursal", "ingrediente_id"], how="left")
    df = df.merge(
        orden_valida[["sucursal", "ingrediente_id", "cantidad_formatos"]],
        on=["sucursal", "ingrediente_id"],
        how="left",
    )
    df = df.merge(ingredientes_df, on="ingrediente_id", how="left")

    # Datos faltantes explícitos (supuestos, ver README):
    df["proyeccion_prox_semana"] = df["proyeccion_prox_semana"].fillna(0.0)
    df["stock_actual_unidad_base"] = df["stock_actual_unidad_base"].fillna(0.0)
    df["cantidad_formatos"] = df["cantidad_formatos"].fillna(0.0)
    df["metodo_proyeccion"] = df["metodo_proyeccion"].fillna("sin_historico")

    # Necesidad real: lo que hay que comprar para llegar a 0 al final de la semana
    df["necesidad_real"] = (df["proyeccion_prox_semana"] - df["stock_actual_unidad_base"]).clip(lower=0)

    # Conversión de formatos -> unidad base
    df["orden_unidad_base"] = df["cantidad_formatos"] * df["unidad_base_por_formato"]

    # Formatos "ideales" = redondeando hacia arriba la necesidad real (no se compra medio saco)
    df["formatos_ideales"] = np.where(
        df["unidad_base_por_formato"] > 0,
        np.ceil((df["necesidad_real"] / df["unidad_base_por_formato"]) - 1e-9),
        0,
    )
    df["diferencia_formatos"] = df["cantidad_formatos"] - df["formatos_ideales"]

    df["diferencia_unidad_base"] = df["orden_unidad_base"] - df["necesidad_real"]

    # Impacto económico estimado (precio_referencia_usd × diferencia en unidad base).
    # Positivo = plata de más inmovilizada en el pedido; negativo = valor que
    # falta cubrir (riesgo de quiebre). Son precios de referencia para tener
    # una magnitud en $, no una facturación real — ver README.
    df["impacto_usd"] = df["diferencia_unidad_base"] * df["precio_referencia_usd"]

    # ¿Fue pedido explícitamente esta semana? (cantidad > 0 — una fila con 0
    # unidades, como las que arma el editor en vivo del dashboard para poder
    # cargar algo que antes no se pedía, NO cuenta como "pedido")
    ids_pedidos_por_sucursal = (
        orden_valida[orden_valida["cantidad_formatos"] > 0]
        .groupby("sucursal")["ingrediente_id"]
        .apply(set)
        .to_dict()
    )
    df["fue_pedido"] = df.apply(
        lambda r: r["ingrediente_id"] in ids_pedidos_por_sucursal.get(r["sucursal"], set()), axis=1
    )

    df["tipo_alerta"], df["severidad"], df["mensaje"] = zip(*df.apply(_clasificar_fila, axis=1))

    return df, orden_desconocida


def _clasificar_fila(r):
    """Devuelve (tipo_alerta, severidad, mensaje) para una fila sucursal+ingrediente."""
    nombre = r.get("nombre", r["ingrediente_id"])
    unidad = r.get("unidad_base", "u")
    perecedero = str(r.get("es_perecedero", "No")).strip().lower() in ("si", "sí", "yes", "true")

    # Caso: no se pidió nada y sí se necesita -> OLVIDO
    if not r["fue_pedido"] and r["necesidad_real"] > 0:
        severidad = "Crítico" if perecedero or r["necesidad_real"] > r["unidad_base_por_formato"] * 2 else "Alerta"
        faltante_fmt = r["necesidad_real"] / r["unidad_base_por_formato"] if r["unidad_base_por_formato"] else 0
        msg = (
            f"ALERTA: {r['sucursal']} no incluyó {nombre} en la orden de esta semana, "
            f"pero se proyecta una necesidad de {r['necesidad_real']:.1f} {unidad} "
            f"(~{faltante_fmt:.1f} {r.get('formato_compra','formatos')}) → riesgo de quiebre."
        )
        return "Olvido", severidad, msg

    # Caso: pidió, pero de menos (más de 1 formato completo por debajo de lo ideal)
    if r["diferencia_formatos"] <= -1:
        faltante = -r["diferencia_unidad_base"]
        cobertura = (
            (r["stock_actual_unidad_base"] + r["orden_unidad_base"]) / r["proyeccion_prox_semana"]
            if r["proyeccion_prox_semana"] > 0
            else 1.0
        )
        severidad = "Crítico" if cobertura < 0.7 else "Alerta"
        msg = (
            f"ALERTA: {r['sucursal']} está pidiendo {faltante:.1f} {unidad} de {nombre} "
            f"menos que lo proyectado → riesgo de quiebre."
        )
        return "Pedido insuficiente", severidad, msg

    # Caso: pidió de más (más de 1 formato completo por encima del ideal)
    if r["diferencia_formatos"] >= 1:
        excedente = r["diferencia_unidad_base"]
        severidad = "Alerta" if perecedero else "Atención"
        msg = (
            f"ALERTA: {r['sucursal']} está pidiendo {excedente:.1f} {unidad} de {nombre} "
            f"más que lo proyectado"
            + (" (perecedero → riesgo de vencimiento)." if perecedero else " → plata inmovilizada en inventario.")
        )
        return "Sobre-pedido", severidad, msg

    # Dentro de la tolerancia de redondeo (menos de 1 formato de diferencia)
    return "OK", "OK", f"{r['sucursal']} - {nombre}: pedido dentro de lo esperado."


# ---------------------------------------------------------------------------
# 6a. DETECCIÓN DE PEDIDOS "RAROS" (una sucursal vs. las demás)
# ---------------------------------------------------------------------------
def detectar_pedidos_atipicos(df, umbral_z=3.0, umbral_diferencia_relativa=0.5):
    """
    Para cada ingrediente, compara la 'intensidad de pedido' de cada sucursal
    (orden convertida / proyección propia) contra las demás sucursales.
    Si una sucursal se aleja mucho del resto (z-score robusto), se marca como atípica.
    Esto atrapa, por ejemplo, una sucursal que pide 3x más queso que las demás
    para un consumo proyectado similar.
    """
    tmp = df.copy()
    tmp["ratio_pedido_vs_proyeccion"] = np.where(
        tmp["proyeccion_prox_semana"] > 0,
        tmp["orden_unidad_base"] / tmp["proyeccion_prox_semana"],
        np.nan,
    )

    filas = []
    for ingrediente_id, grupo in tmp.groupby("ingrediente_id"):
        ratios = grupo["ratio_pedido_vs_proyeccion"].dropna()
        if len(ratios) < 3:
            continue
        mediana = ratios.median()
        mad = (ratios - mediana).abs().median() or 1e-9
        for _, fila in grupo.iterrows():
            ratio = fila["ratio_pedido_vs_proyeccion"]
            if pd.isna(ratio):
                continue
            z = 0.6745 * (ratio - mediana) / mad
            diferencia_relativa = abs(ratio - mediana) / mediana if mediana > 0 else 0
            if abs(z) >= umbral_z and diferencia_relativa >= umbral_diferencia_relativa:
                filas.append(
                    {
                        "sucursal": fila["sucursal"],
                        "ingrediente_id": ingrediente_id,
                        "nombre": fila.get("nombre", ingrediente_id),
                        "ratio_sucursal": ratio,
                        "ratio_mediana_resto": mediana,
                        "mensaje": (
                            f"{fila['sucursal']} pide {ratio:.1f}x su proyección de "
                            f"{fila.get('nombre', ingrediente_id)}, vs. {mediana:.1f}x en el resto de sucursales "
                            f"→ revisar (¿cliente puntual, evento, o error de carga?)."
                        ),
                    }
                )
    return pd.DataFrame(filas)


# ---------------------------------------------------------------------------
# 6b. PEDIDO CORREGIDO AGRUPADO POR PROVEEDOR
# ---------------------------------------------------------------------------
def pedido_corregido_por_proveedor(df):
    """Arma la lista de compra recomendada (formatos ideales, redondeados hacia
    arriba) agrupada por proveedor, lista para reenviar a cada uno."""
    cols = [
        "proveedor",
        "sucursal",
        "nombre",
        "ingrediente_id",
        "formato_compra",
        "cantidad_formatos",
        "formatos_ideales",
    ]
    tabla = df[cols].copy()
    tabla = tabla.rename(
        columns={
            "cantidad_formatos": "formatos_pedidos_original",
            "formatos_ideales": "formatos_recomendados",
        }
    )
    tabla["formatos_recomendados"] = tabla["formatos_recomendados"].astype(int)
    tabla["formatos_pedidos_original"] = tabla["formatos_pedidos_original"].astype(int)
    tabla = tabla[
        (tabla["formatos_pedidos_original"] > 0) | (tabla["formatos_recomendados"] > 0)
    ]
    return tabla.sort_values(["proveedor", "sucursal", "nombre"])


# ---------------------------------------------------------------------------
# 6c. "CHAT" CON LOS DATOS (basado en reglas, funciona 100% gratis y offline)
# ---------------------------------------------------------------------------
def _normalizar(texto):
    """minúsculas + sin tildes, para poder buscar 'mas' == 'más'."""
    texto = str(texto).lower()
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c))


def responder_pregunta(pregunta, df):
    """
    Responde preguntas en español simple sobre las alertas ya calculadas,
    sin necesidad de leer tablas. No usa un LLM externo (para que la demo
    funcione siempre, gratis y sin API key) — busca la sucursal / ingrediente
    mencionados y el tipo de problema, y arma una respuesta en texto con los
    mensajes de alerta ya generados por construir_tabla_maestra().

    (app.py agrega, opcionalmente, una capa con la API de Claude que toma esta
    misma respuesta y la redacta de forma más natural si hay una API key
    configurada — ver sección "IA opcional" en app.py.)
    """
    q = _normalizar(pregunta)

    sucursales = df["sucursal"].unique().tolist()
    sucursal_match = next((s for s in sucursales if _normalizar(s) in q), None)

    ingredientes_unicos = df[["ingrediente_id", "nombre"]].drop_duplicates()
    ingrediente_match = None
    for _, fila in ingredientes_unicos.iterrows():
        nombre_norm = _normalizar(fila["nombre"])
        id_norm = _normalizar(fila["ingrediente_id"]).replace("_", " ")
        palabras_clave = [p for p in nombre_norm.split() if len(p) >= 4]
        if nombre_norm in q or id_norm in q or any(p in q for p in palabras_clave):
            ingrediente_match = fila
            break

    quiere_exceso = any(p in q for p in ["exceso", "de mas", "sobra", "sobrando", "demasiado", "mucho"])
    quiere_falta = any(p in q for p in ["falta", "poco", "quiebre", "insuficiente", "riesgo"])
    quiere_olvido = "olvid" in q
    quiere_raro = any(p in q for p in ["raro", "atipico", "extrano", "sospechoso"])
    quiere_criticos = any(p in q for p in ["critic", "urgente", "peor", "prioridad", "prioritario"])
    quiere_resumen = any(p in q for p in ["resumen", "panorama", "como estamos", "como vamos", "general"])

    # --- caso: "¿qué productos/ingredientes son críticos?" (sin sucursal ni ingrediente puntual) ---
    if quiere_criticos and sucursal_match is None and ingrediente_match is None:
        sub = df[df.severidad == "Crítico"].sort_values("diferencia_unidad_base")
        if len(sub) == 0:
            return "Ninguna alerta está marcada como Crítica esta semana — lo peor que hay son alertas de menor severidad."
        lineas = [f"- {r['mensaje']}" for _, r in sub.iterrows()]
        return f"Hay {len(sub)} alerta(s) crítica(s) esta semana:\n\n" + "\n".join(lineas)

    # --- caso: sucursal + ingrediente puntual -> el detalle exacto ---
    if sucursal_match and ingrediente_match is not None:
        fila = df[(df.sucursal == sucursal_match) & (df.ingrediente_id == ingrediente_match.ingrediente_id)]
        if len(fila):
            f = fila.iloc[0]
            return (
                f"**{sucursal_match} · {f['nombre']}**\n\n"
                f"- Proyección próxima semana: {f['proyeccion_prox_semana']:.1f} {f.get('unidad_base','')}\n"
                f"- Stock actual: {f['stock_actual_unidad_base']:.1f} {f.get('unidad_base','')}\n"
                f"- Necesidad real: {f['necesidad_real']:.1f} {f.get('unidad_base','')}\n"
                f"- Está pidiendo: {f['cantidad_formatos']:.0f} {f.get('formato_compra','formatos')} "
                f"({f['orden_unidad_base']:.1f} {f.get('unidad_base','')})\n"
                f"- Veredicto: **{f['tipo_alerta']}** ({f['severidad']}) — {f['mensaje']}"
            )

    # --- caso: solo ingrediente -> comparar entre sucursales ---
    if ingrediente_match is not None:
        sub = df[df.ingrediente_id == ingrediente_match.ingrediente_id].copy()
        if quiere_exceso:
            sub = sub[sub.tipo_alerta == "Sobre-pedido"].sort_values("diferencia_unidad_base", ascending=False)
        elif quiere_falta or quiere_olvido:
            sub = sub[sub.tipo_alerta.isin(["Pedido insuficiente", "Olvido"])].sort_values("diferencia_unidad_base")
        if len(sub) == 0:
            return f"No encontré alertas de **{ingrediente_match['nombre']}** con esos criterios — parece que todas las sucursales están pidiendo cantidades razonables."
        lineas = [f"- {r['mensaje']}" for _, r in sub.head(6).iterrows()]
        return f"Esto encontré sobre **{ingrediente_match['nombre']}**:\n\n" + "\n".join(lineas)

    # --- caso: solo sucursal ---
    if sucursal_match:
        sub = df[df.sucursal == sucursal_match]
        if quiere_olvido:
            sub = sub[sub.tipo_alerta == "Olvido"]
        elif quiere_falta:
            sub = sub[sub.tipo_alerta == "Pedido insuficiente"]
        elif quiere_exceso:
            sub = sub[sub.tipo_alerta == "Sobre-pedido"]
        else:
            sub = sub[sub.tipo_alerta != "OK"]
        sub = sub.sort_values(
            "severidad", key=lambda s: s.map({"Crítico": 0, "Alerta": 1, "Atención": 2, "OK": 3})
        )
        if len(sub) == 0:
            return f"**{sucursal_match}** no tiene alertas pendientes con esos criterios — su orden de esta semana luce bien."
        lineas = [f"- {r['mensaje']}" for _, r in sub.head(8).iterrows()]
        return f"Alertas de **{sucursal_match}** ({len(sub)} en total, mostrando las principales):\n\n" + "\n".join(lineas)

    # --- caso: pedidos raros entre sucursales ---
    if quiere_raro:
        atipicos = detectar_pedidos_atipicos(df)
        if len(atipicos) == 0:
            return "No detecté pedidos claramente atípicos esta semana comparando las sucursales entre sí."
        lineas = [f"- {r['mensaje']}" for _, r in atipicos.head(6).iterrows()]
        return "Pedidos que se alejan del resto de las sucursales:\n\n" + "\n".join(lineas)

    # --- caso: resumen general / no se entendió nada ---
    conteo = df["severidad"].value_counts()
    top_criticos = df[df.severidad == "Crítico"].head(4)
    resumen = (
        f"**Panorama general de esta semana:**\n\n"
        f"- 🔴 Crítico: {conteo.get('Crítico', 0)}\n"
        f"- 🟠 Alerta: {conteo.get('Alerta', 0)}\n"
        f"- 🟡 Atención: {conteo.get('Atención', 0)}\n"
        f"- 🟢 OK: {conteo.get('OK', 0)}\n"
    )
    if len(top_criticos):
        resumen += "\nLo más urgente:\n" + "\n".join(f"- {r['mensaje']}" for _, r in top_criticos.iterrows())
    else:
        resumen += (
            "\nNo pude identificar una sucursal o ingrediente específico en tu pregunta — "
            "probá mencionando el nombre de una sucursal (ej. *Marbella*) o de un ingrediente "
            "(ej. *mozzarella*), o preguntá algo como *'¿qué sucursal pide demasiada harina?'*."
        )
    return resumen


# ---------------------------------------------------------------------------
# 7. RESUMEN EJECUTIVO Y SEMÁFORO POR SUCURSAL
# ---------------------------------------------------------------------------
def generar_resumen_ejecutivo(df):
    """Un párrafo corto en lenguaje natural para poner arriba de todo el
    dashboard: la idea es que con solo leer esto (sin tocar ninguna tabla)
    la gerente ya sepa si tiene que actuar hoy y en qué."""
    criticos = df[df.severidad == "Crítico"]
    alertas = df[df.severidad == "Alerta"]

    if len(criticos) == 0 and len(alertas) == 0:
        return "✅ No hay alertas críticas ni importantes esta semana — los pedidos de todas las sucursales lucen razonables."

    sucursales_afectadas = sorted(set(criticos["sucursal"]) | set(alertas["sucursal"]))
    inmovilizado = df.loc[df.impacto_usd > 0, "impacto_usd"].sum()
    en_riesgo = -df.loc[df.impacto_usd < 0, "impacto_usd"].sum()

    partes = []
    if len(criticos):
        top = criticos.sort_values("diferencia_unidad_base").iloc[0]
        partes.append(
            f"hay **{len(criticos)} alerta(s) crítica(s)** — la más urgente: "
            f"{top['sucursal']} y {top['nombre']} ({top['tipo_alerta'].lower()})"
        )
    if len(alertas):
        partes.append(f"{len(alertas)} alerta(s) adicionales de menor severidad")

    frase = " y ".join(partes) + "."
    afectadas_txt = ", ".join(sucursales_afectadas[:4]) + ("..." if len(sucursales_afectadas) > 4 else "")

    dinero = ""
    if inmovilizado > 1 or en_riesgo > 1:
        dinero = (
            f" En términos de plata: ~\\${en_riesgo:,.0f} en riesgo por posibles quiebres "
            f"y ~\\${inmovilizado:,.0f} inmovilizados en sobre-pedidos."
        )

    return (
        f"📋 Esta semana {frase} Sucursales que necesitan atención: **{afectadas_txt}**.{dinero}"
    )


def resumen_por_sucursal(df):
    """Una fila por sucursal con la peor severidad presente, cantidad de
    alertas y un mensaje corto — para las tarjetas tipo semáforo."""
    orden_severidad = {"Crítico": 0, "Alerta": 1, "Atención": 2, "OK": 3}
    filas = []
    for sucursal, grupo in df.groupby("sucursal"):
        con_alerta = grupo[grupo.tipo_alerta != "OK"]
        if len(con_alerta) == 0:
            peor = "OK"
            mensaje = "Sin alertas — pedido dentro de lo esperado."
        else:
            con_alerta = con_alerta.sort_values("severidad", key=lambda s: s.map(orden_severidad))
            peor_fila = con_alerta.iloc[0]
            peor = peor_fila["severidad"]
            mensaje = peor_fila["mensaje"].replace("ALERTA: ", "")
        filas.append(
            {
                "sucursal": sucursal,
                "peor_severidad": peor,
                "cantidad_alertas": int(len(con_alerta)),
                "mensaje_principal": mensaje,
            }
        )
    resultado = pd.DataFrame(filas)
    resultado["orden"] = resultado["peor_severidad"].map(orden_severidad)
    return resultado.sort_values("orden").drop(columns="orden")
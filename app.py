"""
app.py — Dashboard de revisión de órdenes de compra · Barrio Pizza
Correr localmente con:  streamlit run app.py
"""

import os

import pandas as pd
import plotly.express as px
import streamlit as st

from data_processing import (
    cargar_datos,
    construir_tabla_maestra,
    detectar_pedidos_atipicos,
    generar_resumen_ejecutivo,
    pedido_corregido_por_proveedor,
    responder_pregunta,
    resumen_por_sucursal,
)

st.set_page_config(page_title="Barrio Pizza · Revisor de Órdenes", page_icon="🍕", layout="wide")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

COLOR_SEVERIDAD = {
    "Crítico": "background-color:#8b1e1e; color:#ffffff; font-weight:600",
    "Alerta": "background-color:#8a4b12; color:#ffffff; font-weight:600",
    "Atención": "background-color:#8a7a12; color:#ffffff; font-weight:600",
    "OK": "background-color:#1e6b32; color:#ffffff; font-weight:600",
}


def _mensaje_corto(fila):
    """Versión resumida del mensaje, sin repetir sucursal/ingrediente
    (que ya están en sus propias columnas) — para que la tabla no se
    desborde de ancho."""
    unidad = fila.get("unidad_base", "")
    perecedero = str(fila.get("es_perecedero", "No")).strip().lower() in ("si", "sí", "yes", "true")

    if fila["tipo_alerta"] == "Olvido":
        return f"No lo pidió · necesita ~{fila['necesidad_real']:.1f} {unidad} → riesgo de quiebre"
    if fila["tipo_alerta"] == "Pedido insuficiente":
        return f"Pide {abs(fila['diferencia_unidad_base']):.1f} {unidad} de menos → riesgo de quiebre"
    if fila["tipo_alerta"] == "Sobre-pedido":
        motivo = "riesgo de vencimiento" if perecedero else "plata inmovilizada"
        return f"Pide {fila['diferencia_unidad_base']:.1f} {unidad} de más → {motivo}"
    return "Dentro de lo esperado"


def _icono_severidad(sev):
    return {"Crítico": "🔴", "Alerta": "🟠", "Atención": "🟡", "OK": "🟢"}.get(sev, "⚪")


# ---------------------------------------------------------------------------
# Carga de datos (con opción de subir archivos propios)
# ---------------------------------------------------------------------------
@st.cache_data
def leer_csv_default():
    ing = pd.read_csv(f"{DATA_DIR}/ingredientes.csv")
    cons = pd.read_csv(f"{DATA_DIR}/consumo_historico.csv")
    inv = pd.read_csv(f"{DATA_DIR}/inventario_actual.csv")
    ordn = pd.read_csv(f"{DATA_DIR}/orden_compra_semana.csv")
    return ing, cons, inv, ordn


st.sidebar.title("🍕 BARRIO PIZZA")
st.sidebar.caption("Revisor automático de órdenes de compra semanales")

with st.sidebar.expander("📁 CARGAR MIS PROPIOS DATOS"):
    st.caption("Por defecto se usan los 4 CSV de ejemplo de las 4 sucursales. Subí los tuyos para reemplazarlos.")
    # Nota: estas 4 etiquetas quedan como nombres de archivo literales
    # (ingredientes.csv, etc.) a propósito — son el nombre real que el
    # archivo tiene que tener, cambiarlas confundiría qué subir.
    up_ing = st.file_uploader("ingredientes.csv", type="csv")
    up_cons = st.file_uploader("consumo_historico.csv", type="csv")
    up_inv = st.file_uploader("inventario_actual.csv", type="csv")
    up_ord = st.file_uploader("orden_compra_semana.csv", type="csv")

ing_default, cons_default, inv_default, ord_default = leer_csv_default()

ingredientes_raw = pd.read_csv(up_ing) if up_ing else ing_default
consumo_raw = pd.read_csv(up_cons) if up_cons else cons_default
inventario_raw = pd.read_csv(up_inv) if up_inv else inv_default
orden_raw = pd.read_csv(up_ord) if up_ord else ord_default

ingredientes_df, consumo_df, inventario_df, orden_df = cargar_datos(
    ingredientes_raw, consumo_raw, inventario_raw, orden_raw
)

# Tabla base (con la orden ORIGINAL) — se usa para armar el editor y para
# detectar ingredientes no catalogados en la orden (problema de datos)
df_base, orden_desconocida = construir_tabla_maestra(ingredientes_df, consumo_df, inventario_df, orden_df)

# ---------------------------------------------------------------------------
# Estado editable de la orden (para el tab "Editar orden")
# Se reinicia solo si cambian los datos de origen (nuevo upload) o si el
# usuario aprieta "reiniciar ediciones".
# ---------------------------------------------------------------------------
hash_actual = pd.util.hash_pandas_object(orden_raw).sum()
if st.session_state.get("hash_orden") != hash_actual:
    st.session_state.orden_trabajo = df_base[
        ["sucursal", "ingrediente_id", "nombre", "proveedor", "formato_compra", "cantidad_formatos"]
    ].copy()
    st.session_state.hash_orden = hash_actual

sucursales_todas = sorted(df_base["sucursal"].unique())

st.sidebar.divider()
st.sidebar.subheader("FILTROS")
f_sucursales = st.sidebar.multiselect("Sucursal", sucursales_todas, default=sucursales_todas)
mostrar_ok = st.sidebar.checkbox("Mostrar También Los Pedidos OK", value=False)

# ---------------------------------------------------------------------------
# Espacio reservado para el panorama ejecutivo (resumen + semáforo + KPIs).
# Se crea ACÁ (para que aparezca arriba de todo, antes de los tabs) pero se
# llena más abajo, una vez que ya tenemos la orden recalculada — incluidas
# las ediciones en vivo que se hagan en la pestaña "Editar orden".
# ---------------------------------------------------------------------------
panorama_ejecutivo = st.container()
st.divider()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_alertas, tab_editar, tab_panorama, tab_proveedor, tab_atipicos, tab_chat, tab_calidad = st.tabs(
    ["🚨 ALERTAS", "✏️ EDITAR ORDEN", "📊 PANORAMA", "📦 PEDIDO POR PROVEEDOR", "🕵️ PEDIDOS ATÍPICOS", "💬 PREGUNTALE A LOS DATOS", "⚠️ CALIDAD DE DATOS"]
)

# --- Tab: Editar orden (se ejecuta primero para que el resto use los cambios) ---
with tab_editar:
    st.subheader("EDITAR LA ORDEN DE ESTA SEMANA")
    st.caption(
        "Cambiá las cantidades (en formatos de compra) y las alertas de las otras pestañas se recalculan solas. "
        "También podés cargarle una cantidad a un ingrediente que la sucursal no había pedido (fila en 0)."
    )
    col1, col2 = st.columns([3, 1])
    with col1:
        sucursal_editar = st.selectbox("Sucursal A Editar", ["Todas"] + sucursales_todas, key="sel_editar")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 Reiniciar Ediciones"):
            st.session_state.orden_trabajo = df_base[
                ["sucursal", "ingrediente_id", "nombre", "proveedor", "formato_compra", "cantidad_formatos"]
            ].copy()
            st.rerun()

    vista = st.session_state.orden_trabajo
    if sucursal_editar != "Todas":
        vista = vista[vista.sucursal == sucursal_editar]

    columnas_editor = ["sucursal", "proveedor", "nombre", "formato_compra", "cantidad_formatos"]
    editado = st.data_editor(
        vista[columnas_editor],
        disabled=["sucursal", "proveedor", "nombre", "formato_compra"],
        column_config={
            "sucursal": st.column_config.TextColumn("SUCURSAL"),
            "proveedor": st.column_config.TextColumn("PROVEEDOR"),
            "nombre": st.column_config.TextColumn("INGREDIENTE"),
            "formato_compra": st.column_config.TextColumn("FORMATO COMPRA"),
            "cantidad_formatos": st.column_config.NumberColumn("CANTIDAD FORMATOS", min_value=0, step=1),
        },
        hide_index=True,
        use_container_width=True,
        key=f"editor_{sucursal_editar}",
    )
    # escribir los cambios de vuelta al estado global (mismo índice que 'vista')
    st.session_state.orden_trabajo.loc[editado.index, "cantidad_formatos"] = editado["cantidad_formatos"].values

# Recalcular con la orden (posiblemente editada). Ojo: NO reasignamos
# "orden_desconocida" acá — el editor en vivo solo trabaja sobre ingredientes
# que SÍ están en el catálogo, así que una segunda pasada por acá siempre
# daría "0 desconocidos" y taparía el problema real que ya detectamos arriba
# contra el archivo original.
orden_trabajo_para_calculo = st.session_state.orden_trabajo[["sucursal", "ingrediente_id", "cantidad_formatos"]]
df, _ = construir_tabla_maestra(ingredientes_df, consumo_df, inventario_df, orden_trabajo_para_calculo)

df_filtrado = df[df["sucursal"].isin(f_sucursales)]
if not mostrar_ok:
    df_visible = df_filtrado[df_filtrado["tipo_alerta"] != "OK"]
else:
    df_visible = df_filtrado

orden_severidad = {"Crítico": 0, "Alerta": 1, "Atención": 2, "OK": 3}
df_visible = df_visible.sort_values("severidad", key=lambda s: s.map(orden_severidad))

# --- Llenar el panorama ejecutivo (definido arriba, antes de los tabs) ---
with panorama_ejecutivo:
    st.subheader("📋 PANORAMA EJECUTIVO")
    st.info(generar_resumen_ejecutivo(df_filtrado))

    en_riesgo_usd = -df_filtrado.loc[df_filtrado.impacto_usd < 0, "impacto_usd"].sum()
    inmovilizado_usd = df_filtrado.loc[df_filtrado.impacto_usd > 0, "impacto_usd"].sum()

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("🔴 Críticas", int((df_filtrado.severidad == "Crítico").sum()))
    k2.metric("🟠 Alertas", int((df_filtrado.severidad == "Alerta").sum()))
    k3.metric("🟡 Atención", int((df_filtrado.severidad == "Atención").sum()))
    k4.metric("Sucursales Con Crítico", df_filtrado[df_filtrado.severidad == "Crítico"]["sucursal"].nunique())
    k5.metric("💰 $ En Riesgo", f"${en_riesgo_usd:,.0f}")
    k6.metric("📦 $ Inmovilizado", f"${inmovilizado_usd:,.0f}")

    st.caption("Semáforo por sucursal — la peor alerta pendiente de cada una:")
    resumen_suc = resumen_por_sucursal(df_filtrado)
    cols_suc = st.columns(len(resumen_suc)) if len(resumen_suc) else []
    for col, (_, fila) in zip(cols_suc, resumen_suc.iterrows()):
        with col:
            with st.container(border=True):
                st.markdown(f"**{_icono_severidad(fila['peor_severidad'])} {fila['sucursal'].upper()}**")
                st.caption(f"{fila['cantidad_alertas']} alerta(s) · {fila['peor_severidad']}")
                texto = fila["mensaje_principal"]
                st.caption(texto if len(texto) <= 100 else texto[:97] + "...")

# --- Tab: Alertas ---
with tab_alertas:
    st.subheader("ALERTAS DE LA SEMANA")
    if len(df_visible) == 0:
        st.success("No hay alertas con los filtros actuales. 🎉")
    else:
        tabla = df_visible.copy()
        tabla["detalle"] = tabla.apply(_mensaje_corto, axis=1)

        tabla = tabla[
            ["sucursal", "nombre", "severidad", "tipo_alerta", "detalle", "necesidad_real", "orden_unidad_base", "impacto_usd"]
        ].rename(columns={
            "nombre": "ingrediente",
            "tipo_alerta": "tipo",
            "necesidad_real": "necesidad",
            "orden_unidad_base": "pedido",
            "impacto_usd": "impacto $",
        })
        for col in ["necesidad", "pedido", "impacto $"]:
            tabla[col] = tabla[col].round(1)
        # "Pedido insuficiente" -> "Pedido Insuficiente" (solo para mostrar;
        # el valor interno de tipo_alerta no cambia, así los filtros del
        # resto del código siguen funcionando igual)
        tabla["tipo"] = tabla["tipo"].str.title()

        # Títulos en mayúsculas, sin guiones bajos
        tabla.columns = [c.upper().replace("_", " ") for c in tabla.columns]

        estilo = (
            tabla.style
            .apply(lambda s: [COLOR_SEVERIDAD.get(v, "") for v in tabla["SEVERIDAD"]], subset=["SEVERIDAD"])
            .format({"NECESIDAD": "{:.1f}", "PEDIDO": "{:.1f}", "IMPACTO $": "${:,.1f}"})
        )

        st.caption("💡 Hacé clic en una fila para ver el detalle del cálculo (histórico + proyección) abajo.")
        evento = st.dataframe(
            estilo,
            use_container_width=True,
            hide_index=True,
            height=520,
            column_config={
                "DETALLE": st.column_config.TextColumn(width="large"),
                "INGREDIENTE": st.column_config.TextColumn(width="medium"),
            },
            on_select="rerun",
            selection_mode="single-row",
            key="tabla_alertas_selector",
        )

        # --- Transparencia del cálculo: al hacer clic en una fila, mostramos
        # las 6 semanas de histórico + la proyección detrás de esa alerta ---
        filas_sel = evento.selection.rows if evento and evento.selection else []
        if filas_sel:
            fila_idx = tabla.index[filas_sel[0]]
            f = df_visible.loc[fila_idx]
            unidad = f.get("unidad_base", "")
            metodo_legible = str(f.get("metodo_proyeccion", "")).replace("_", " ").title()
            st.markdown(f"#### 🔍 DETALLE DEL CÁLCULO — {f['sucursal'].upper()} · {f['nombre'].upper()}")
            col_g, col_d = st.columns([2, 1])
            with col_g:
                valores = [f.get(s) for s in ["S1", "S2", "S3", "S4", "S5", "S6"]]
                datos_grafico = pd.DataFrame({
                    "semana": ["S1", "S2", "S3", "S4", "S5", "S6", "Proyección S7"],
                    "valor": valores + [f["proyeccion_prox_semana"]],
                    "tipo": ["Histórico"] * 6 + ["Proyección"],
                })
                fig_detalle = px.bar(
                    datos_grafico, x="semana", y="valor", color="tipo",
                    color_discrete_map={"Histórico": "#4c78a8", "Proyección": "#e05252"},
                    title=f"CONSUMO SEMANAL — {f['nombre'].upper()} ({metodo_legible.upper()})",
                    labels={"semana": "Semana", "valor": f"Consumo ({unidad})", "tipo": "Tipo"},
                )
                st.plotly_chart(fig_detalle, use_container_width=True)
            with col_d:
                st.metric("Proyección Próx. Semana", f"{f['proyeccion_prox_semana']:.1f} {unidad}")
                st.metric("Stock Actual", f"{f['stock_actual_unidad_base']:.1f} {unidad}")
                st.metric("Necesidad Real", f"{f['necesidad_real']:.1f} {unidad}")
                st.metric("Pedido Esta Semana", f"{f['orden_unidad_base']:.1f} {unidad}")
                st.metric("Impacto Estimado", f"${f['impacto_usd']:,.1f}")

    if len(orden_desconocida):
        st.warning(
            f"⚠️ Hay {len(orden_desconocida)} línea(s) de pedido con ingredientes que no existen en el catálogo "
            "— revisá la pestaña 'Calidad de datos'."
        )

# --- Tab: Panorama (gráficos) ---
with tab_panorama:
    st.subheader("PANORAMA GENERAL")
    colA, colB = st.columns(2)

    with colA:
        conteo = (
            df_filtrado[df_filtrado.tipo_alerta != "OK"]
            .groupby(["sucursal", "severidad"]).size().reset_index(name="cantidad")
        )
        if len(conteo):
            fig = px.bar(
                conteo, x="sucursal", y="cantidad", color="severidad",
                color_discrete_map={"Crítico": "#e05252", "Alerta": "#f2a154", "Atención": "#f2d43f"},
                title="ALERTAS POR SUCURSAL",
                labels={"sucursal": "Sucursal", "cantidad": "Cantidad", "severidad": "Severidad"},
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Sin alertas para graficar con los filtros actuales.")

    with colB:
        peores_faltantes = (
            df_filtrado[df_filtrado.tipo_alerta.isin(["Pedido insuficiente", "Olvido"])]
            .nsmallest(8, "diferencia_unidad_base")
        )
        if len(peores_faltantes):
            fig2 = px.bar(
                peores_faltantes, x="diferencia_unidad_base", y="nombre", color="sucursal",
                orientation="h", title="MAYORES RIESGOS DE QUIEBRE (UNIDAD BASE FALTANTE)",
                labels={"diferencia_unidad_base": "Faltante (Unidad Base)", "nombre": "Ingrediente", "sucursal": "Sucursal"},
            )
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No hay riesgos de quiebre con los filtros actuales.")

    peores_excesos = (
        df_filtrado[df_filtrado.tipo_alerta == "Sobre-pedido"]
        .nlargest(8, "diferencia_unidad_base")
    )
    if len(peores_excesos):
        fig3 = px.bar(
            peores_excesos, x="diferencia_unidad_base", y="nombre", color="sucursal",
            orientation="h", title="MAYORES EXCEDENTES (PLATA INMOVILIZADA / RIESGO DE VENCIMIENTO)",
            labels={"diferencia_unidad_base": "Excedente (Unidad Base)", "nombre": "Ingrediente", "sucursal": "Sucursal"},
        )
        st.plotly_chart(fig3, use_container_width=True)

    st.divider()
    st.markdown("**IMPACTO ECONÓMICO ESTIMADO** _(con precios de referencia — ver README)_")
    top_impacto = df_filtrado[df_filtrado.tipo_alerta != "OK"].reindex(
        df_filtrado[df_filtrado.tipo_alerta != "OK"]["impacto_usd"].abs().sort_values(ascending=False).index
    ).head(10)
    if len(top_impacto):
        fig4 = px.bar(
            top_impacto, x="impacto_usd", y="nombre", color="sucursal",
            orientation="h", title="TOP 10 ALERTAS POR IMPACTO EN $ (POSITIVO = PLATA INMOVILIZADA, NEGATIVO = VALOR EN RIESGO)",
            labels={"impacto_usd": "Impacto ($)", "nombre": "Ingrediente", "sucursal": "Sucursal"},
        )
        st.plotly_chart(fig4, use_container_width=True)
    else:
        st.info("Sin alertas con impacto económico para graficar.")

# --- Tab: Pedido corregido por proveedor ---
with tab_proveedor:
    st.subheader("PEDIDO RECOMENDADO, AGRUPADO POR PROVEEDOR")
    st.caption(
        "Formatos recomendados = necesidad real redondeada hacia arriba al formato de compra completo. "
        "Listo para reenviarle a cada proveedor su parte."
    )
    tabla_prov = pedido_corregido_por_proveedor(df_filtrado)
    for proveedor, grupo in tabla_prov.groupby("proveedor"):
        with st.expander(f"📦 {proveedor.upper()} ({len(grupo)} LÍNEAS)"):
            grupo_mostrar = grupo.drop(columns=["proveedor", "ingrediente_id"]).rename(columns={
                "sucursal": "SUCURSAL",
                "nombre": "INGREDIENTE",
                "formato_compra": "FORMATO COMPRA",
                "formatos_pedidos_original": "FORMATOS PEDIDOS ORIGINAL",
                "formatos_recomendados": "FORMATOS RECOMENDADOS",
            })
            st.dataframe(grupo_mostrar, use_container_width=True, hide_index=True)

    csv = tabla_prov.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Descargar Pedido Corregido (CSV)", csv, "pedido_corregido_por_proveedor.csv", "text/csv")

# --- Tab: Pedidos atípicos ---
with tab_atipicos:
    st.subheader("SUCURSALES QUE PIDEN DISTINTO AL RESTO")
    st.caption(
        "Compara, para cada ingrediente, cuánto pide cada sucursal en relación a su propia proyección de consumo, "
        "y marca las que se alejan mucho de las demás."
    )
    atipicos = detectar_pedidos_atipicos(df_filtrado)
    if len(atipicos) == 0:
        st.info("No se detectaron pedidos claramente atípicos con los filtros actuales.")
    else:
        for _, r in atipicos.iterrows():
            st.warning(r["mensaje"])

# --- Tab: Chat con los datos ---
with tab_chat:
    st.subheader("PREGUNTALE A LOS DATOS")
    st.caption(
        "Ej: '¿qué sucursal está pidiendo demasiado queso?', '¿Marbella se olvidó de algo?', "
        "'¿hay pedidos raros esta semana?'"
    )

    if "mensajes_chat" not in st.session_state:
        st.session_state.mensajes_chat = []

    for autor, texto in st.session_state.mensajes_chat:
        with st.chat_message(autor):
            st.markdown(texto)

    pregunta = st.chat_input("Escribí tu pregunta...")
    if pregunta:
        st.session_state.mensajes_chat.append(("user", pregunta))
        with st.chat_message("user"):
            st.markdown(pregunta)

        respuesta = responder_pregunta(pregunta, df_filtrado)

        # --- IA opcional: si hay una ANTHROPIC_API_KEY configurada en los
        # secrets de Streamlit, se usa Claude para redactar la respuesta de
        # forma más natural (mismos datos, mejor redacción). Si no hay key,
        # o falla algo, se usa igual la respuesta basada en reglas de arriba
        # -- así el chat nunca se rompe ni requiere pagar nada.
        try:
            api_key = st.secrets.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY"))
        except Exception:
            # No hay archivo secrets.toml configurado -> seguimos con la
            # respuesta basada en reglas (comportamiento normal y esperado).
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            try:
                import anthropic

                cliente = anthropic.Anthropic(api_key=api_key)
                mensaje_ia = cliente.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=400,
                    messages=[{
                        "role": "user",
                        "content": (
                            "Sos el asistente de compras de Barrio Pizza. Reescribí esta respuesta en "
                            "español, tono profesional pero cercano, sin inventar datos nuevos, "
                            f"basándote solo en esto:\n\n{respuesta}\n\nPregunta original: {pregunta}"
                        ),
                    }],
                )
                respuesta = mensaje_ia.content[0].text
            except Exception:
                pass  # si algo falla, nos quedamos con la respuesta basada en reglas

        st.session_state.mensajes_chat.append(("assistant", respuesta))
        with st.chat_message("assistant"):
            st.markdown(respuesta)

# --- Tab: Calidad de datos ---
with tab_calidad:
    st.subheader("PROBLEMAS DE CALIDAD DE DATOS DETECTADOS")
    if len(orden_desconocida) == 0:
        st.success("No se detectaron ingredientes pedidos que falten en el catálogo.")
    else:
        st.write("Estas líneas de la orden mencionan ingredientes que **no existen en `ingredientes.csv`**:")
        orden_desconocida_mostrar = orden_desconocida.copy()
        orden_desconocida_mostrar["ingrediente_id"] = (
            orden_desconocida_mostrar["ingrediente_id"].str.replace("_", " ").str.title()
        )
        orden_desconocida_mostrar = orden_desconocida_mostrar.rename(columns={
            "sucursal": "SUCURSAL",
            "ingrediente_id": "INGREDIENTE (NO CATALOGADO)",
            "cantidad_formatos": "CANTIDAD FORMATOS",
        })
        st.dataframe(orden_desconocida_mostrar, use_container_width=True, hide_index=True)
        st.caption(
            "No se pueden convertir a unidad base (no sabemos su formato de compra) ni comparar contra "
            "consumo histórico, así que se excluyen del análisis de necesidad. Puede ser un insumo nuevo "
            "sin dar de alta en el catálogo, o un error de tipeo en el ingrediente_id."
        )

    sin_historico = df[df.metodo_proyeccion == "sin_historico"]
    if len(sin_historico):
        st.write("Combinaciones sucursal + ingrediente **sin historial de consumo** (se asumió proyección = 0):")
        st.dataframe(
            sin_historico[["sucursal", "nombre"]].rename(columns={"sucursal": "SUCURSAL", "nombre": "INGREDIENTE"}),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Todas las combinaciones sucursal + ingrediente tienen las 6 semanas de historial necesarias.")

# 🍕 Barrio Pizza — Revisor automático de órdenes de compra

Dashboard que revisa las órdenes de compra semanales de las sucursales, proyecta
cuánto va a necesitar realmente cada una, y muestra alertas claras cuando piden
de más, de menos, o se olvidan de algo.

## Cómo correrlo localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

Se abre en `http://localhost:8501`. Ya viene con los 4 CSV de ejemplo cargados
(carpeta `data/`); también podés subir tus propios archivos desde el panel
lateral ("📁 Cargar mis propios datos") sin tocar el código.

## Cómo publicarlo gratis

[Streamlit Community Cloud](https://streamlit.io/cloud): conectás el repo de
GitHub, elegís `app.py` como archivo principal, y listo. No hace falta pagar nada.

## Qué hace, paso a paso

1. **Proyección de consumo** (`data_processing.py::proyectar_consumo`)
   Para cada sucursal + ingrediente, toma las 6 semanas de histórico y:
   - detecta y descarta semanas atípicas (outliers) con un método robusto (MAD),
     para que una sola semana rara no arruine la proyección;
   - ajusta una recta de tendencia (regresión lineal) sobre las semanas
     restantes, para captar si el consumo viene subiendo o bajando, no solo
     su promedio;
   - acota el resultado para que una tendencia calculada con solo 6 puntos no
     extrapole a algo absurdo.
2. **Necesidad real** = `max(0, proyección − stock actual)`.
3. **Conversión de unidades**: la orden viene en formatos de compra (ej. "3
   sacos"), y el consumo/inventario en unidad base (kg, L, unidades). Se
   convierte todo a unidad base usando `unidad_base_por_formato` de
   `ingredientes.csv` antes de comparar nada.
4. **Comparación con tolerancia de redondeo**: como no se puede comprar medio
   saco, se calcula el "formato ideal" (necesidad real redondeada hacia
   arriba) y se compara contra lo que realmente pidieron. Una diferencia de
   menos de 1 formato completo se considera redondeo normal, no una alerta.
5. **Alertas**, con severidad (Crítico / Alerta / Atención):
   - **Pedido insuficiente** → riesgo de quiebre.
   - **Sobre-pedido** → plata inmovilizada, o riesgo de vencimiento si es perecedero.
   - **Olvido** → la sucursal necesita el ingrediente y no lo pidió.
   - Se marcan como **Crítico** los quiebres de insumos perecederos o donde el
     stock + lo pedido cubre menos del 70% de lo proyectado.

## Funcionalidades extra que agregué

- ✅ **Proyección con tendencia + detección de outliers** (no un promedio simple).
- ✅ **"Chat con los datos"**: preguntás en español normal (ej. *"¿qué sucursal
  pide demasiado queso?"*) y responde en texto. Funciona 100% offline con
  reglas (matching de sucursal/ingrediente + intención), así que nunca falla
  ni requiere pagar una API. Si se configura una `ANTHROPIC_API_KEY` en los
  secrets de Streamlit, usa Claude para redactar la misma respuesta de forma
  más natural — ver sección de IA más abajo.
- ✅ **Detección de pedidos atípicos**: compara cada sucursal contra las demás
  (cuánto pide vs. su propia proyección) y marca las que se alejan mucho del
  resto — útil para detectar un evento puntual o un error de carga.
- ✅ **Pedido corregido agrupado por proveedor**, listo para reenviar, con
  botón de descarga en CSV.
- ✅ **Edición en vivo de la orden** desde la misma interfaz (pestaña "Editar
  orden"): cambiás cantidades — incluso agregarle cantidad a algo que no
  habían pedido — y las alertas de todas las pestañas se recalculan solas.
  Esto se acerca a la visión final: cargar la orden de la semana y ver las
  alertas al instante.
- ✅ **Panel de calidad de datos**: detecta ingredientes pedidos que no
  existen en el catálogo (ej. un ingrediente con typo o sin dar de alta) y
  combinaciones sucursal+ingrediente sin historial de consumo, en vez de
  romper o ignorarlos silenciosamente.
- ✅ **Panorama ejecutivo** arriba de todo el dashboard: un resumen en
  lenguaje natural generado automáticamente ("esta semana hay 2 alertas
  críticas... sucursales que necesitan atención: ...") y un semáforo con una
  tarjeta por sucursal (🔴🟠🟡🟢) con su peor alerta pendiente — para que la
  gerente entienda el estado general sin leer ninguna tabla.
- ✅ **Impacto económico estimado**: cada alerta se traduce a un monto en $
  (plata inmovilizada en sobre-pedidos, o valor en riesgo por posibles
  quiebres), con KPIs de $ totales y un gráfico de las alertas con mayor
  impacto. Usa precios de referencia estimados — ver supuestos abajo.
- ✅ **Transparencia del cálculo**: al hacer clic en cualquier fila de la
  tabla de alertas, se despliega un gráfico con las 6 semanas de consumo
  histórico + la proyección calculada para esa sucursal/ingrediente, además
  del desglose completo (proyección, stock, necesidad, pedido, impacto). Así
  la proyección "inteligente" deja de ser una caja negra.

## Supuestos que hice

- La "próxima semana" a proyectar es la semana 7, inmediatamente después de S6.
- Cuando una sucursal no pidió nada de un ingrediente que sí necesita según
  la proyección, se trata como una alerta de "Olvido" (no como pedido = 0
  válido).
- Un ingrediente pedido que no aparece en `ingredientes.csv` (pasa con
  `aji_chombo` en los datos de ejemplo, pedido por Costa del Este) no se
  puede convertir a unidad base ni evaluar — se excluye del cálculo de
  necesidad y se muestra aparte como problema de calidad de datos.
- Si faltara historial de consumo para alguna combinación sucursal +
  ingrediente, se asume proyección = 0 en vez de romper el cálculo (no pasa
  con los datos de ejemplo, pero el código lo contempla).
- "Sobre-pedido" y "pedido insuficiente" se miden en **formatos de compra
  completos**, no en unidad base, porque así es como realmente se compra.
- **Precios de referencia (`precio_referencia_usd` en `ingredientes.csv`)
  son estimaciones mías**, no precios reales de los proveedores — los agregué
  para poder mostrar el impacto económico de cada alerta en dólares (Panamá
  usa USD/Balboa a la par). En una implementación real, esa columna vendría
  del costo real de compra de cada proveedor.

## Cómo llevar esto a producción con Odoo

Hoy el dashboard lee 4 CSV. En producción, esos mismos 4 insumos ya existen
en Odoo y se podrían traer así:

- **`ingredientes.csv`** → módulo de Inventario de Odoo (`product.product` /
  `product.template`), con el formato de compra como una Unidad de Medida de
  compra distinta a la de stock (Odoo ya soporta UdM de compra vs. UdM de
  stock con factor de conversión, igual que `unidad_base_por_formato` acá).
- **`inventario_actual.csv`** → `stock.quant` filtrado por almacén/sucursal.
- **`consumo_historico.csv`** → se puede reconstruir desde `stock.move`
  (salidas de inventario por sucursal) de las últimas semanas, o desde las
  líneas de venta si el consumo se infiere de recetas.
- **`orden_compra_semana.csv`** → líneas de una Orden de Compra (`purchase.order.line`)
  en estado borrador, antes de confirmarla.

La integración se haría con un **módulo custom de Odoo o un script que llame
a su API (XML-RPC/JSON-RPC)**: al crear/editar una orden de compra en borrador,
se dispara la misma lógica de `data_processing.py`, y las alertas se muestran
como un botón/widget dentro del formulario de la orden en Odoo (o se bloquea
la confirmación si hay alertas críticas sin revisar). El modelo de proyección
y las reglas de alerta no cambian — solo cambia de dónde vienen los datos.

## Cómo usé IA para resolver esto

Usé Claude para:
- Explorar los 4 CSV, detectar la estructura real de los datos y los casos
  raros que había que manejar (el ingrediente `aji_chombo` que no está en el
  catálogo, y que Brisas del Golf no pidió mozzarella esa semana).
- Diseñar y escribir la lógica de proyección (outliers con MAD + regresión
  lineal acotada) y las reglas de alerta con tolerancia de redondeo.
- Armar el dashboard en Streamlit (tabs, edición en vivo, gráficos, chat
  basado en reglas) y probarlo de punta a punta antes de entregarlo.
- Redactar este README.

Todo el código fue revisado y probado por mí contra los datos reales antes de
darlo por bueno (ver la sección de supuestos arriba, que documenta las
decisiones que tomé).

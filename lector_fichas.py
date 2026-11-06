"""
Lectura de fichas de producto.

Descarga la página de una tienda y deduce si la consola está disponible,
a qué precio y quién la vende. Orden de fiabilidad:
  1. Datos estructurados JSON-LD (schema.org Product/Offer), que casi todas
     las tiendas publican para Google.
  2. Microdatos y metaetiquetas de producto (itemprop, og:/product:).
  3. Frases visibles en la página, solo si no hay datos estructurados.
Si nada es concluyente, la tienda queda como "desconocido" (no verificable):
nunca se da por agotada ni por disponible sin pruebas.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

from curl_cffi import requests as http

TIMEOUT_SEGUNDOS = 25
MAX_BYTES_PAGINA = 6_000_000  # tope de seguridad por página
COMPRABLES = ("disponible", "reserva")

# Valores de schema.org agrupados en los estados que usa el vigilante
ESTADOS_SCHEMA = {
    "instock": "disponible", "onlineonly": "disponible", "limitedavailability": "disponible",
    "preorder": "reserva", "presale": "reserva", "backorder": "reserva",
    "instoreonly": "solo_tienda",
    "outofstock": "agotado", "soldout": "agotado", "discontinued": "agotado",
}
# Valores habituales en las metaetiquetas og:availability / product:availability
ESTADOS_META = {
    "instock": "disponible", "in stock": "disponible", "available": "disponible",
    "preorder": "reserva", "pre-order": "reserva", "available for order": "reserva",
    "oos": "agotado", "out of stock": "agotado", "outofstock": "agotado",
}
# Frases típicas de las páginas de verificación anti-bots
SENALES_ANTIBOT = (
    "just a moment", "attention required", "access denied", "acceso denegado",
    "are you a robot", "eres un robot", "verify you are human",
    "verifica que eres humano", "pardon our interruption", "request unsuccessful",
)
TIPOS_PRODUCTO = {"product", "individualproduct", "productmodel", "productgroup"}


@dataclass
class Lectura:
    """Resultado de revisar una tienda."""
    tienda: str
    url: str
    estado: str = "desconocido"  # disponible | reserva | cola | solo_tienda | agotado | desconocido
    precio: float | None = None
    vendedor: str | None = None
    vendedor_externo: bool = False
    fuente: str = ""   # json-ld | microdatos | meta | texto | url
    detalle: str = ""  # explicación legible cuando hace falta


@dataclass
class _Oferta:
    estado: str | None
    precio: float | None
    vendedor: str | None
    externo: bool


def normalizar(texto: str) -> str:
    """'El Corte Inglés' -> 'elcorteingles' (minúsculas, sin tildes ni signos)."""
    ascii_ = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_.lower())


def a_euros(valor) -> float | None:
    """Convierte '1.489,99', '899.00', '899,00 €' o 899 en número. None si no se puede."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = re.sub(r"[^\d,.]", "", str(valor))
    if not s:
        return None
    if "," in s and "." in s:  # el último separador es el decimal
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:  # '899,00' decimal; '1,489' miles
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    elif s.count(".") > 1 or len(s.split(".")[-1]) == 3:  # '1.489' o '1.489.000' son miles
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


class _ExtractorHTML(HTMLParser):
    """Recoge JSON-LD, metaetiquetas, microdatos, título y texto visible."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bloques_jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.microdatos: dict[str, list[str]] = {}
        self.titulo = ""
        self._texto: list[str] = []
        self._buffer: list[str] = []
        self._en_jsonld = self._oculto = self._en_titulo = False
        self._capturar: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            self._en_jsonld = "ld+json" in a.get("type", "").lower()
            self._oculto = not self._en_jsonld
            self._buffer = []
            return
        if tag == "style":
            self._oculto = True
            return
        if tag == "title":
            self._en_titulo = True
        if tag == "meta":
            clave = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if clave and a.get("content"):
                self.meta.setdefault(clave, a["content"])
        prop = a.get("itemprop", "").lower()
        if prop in ("price", "availability"):
            valor = a.get("content") or a.get("href")
            if valor:
                self.microdatos.setdefault(prop, []).append(valor)
            elif tag != "meta":
                self._capturar = prop  # el valor viene como texto del elemento

    def handle_endtag(self, tag):
        if tag == "script":
            if self._en_jsonld:
                self.bloques_jsonld.append("".join(self._buffer))
            self._en_jsonld = self._oculto = False
        elif tag == "style":
            self._oculto = False
        elif tag == "title":
            self._en_titulo = False

    def handle_data(self, data):
        if self._en_jsonld:
            self._buffer.append(data)
            return
        if self._oculto:
            return
        if self._en_titulo:
            self.titulo += data
        if self._capturar and data.strip():
            self.microdatos.setdefault(self._capturar, []).append(data.strip())
            self._capturar = None
        self._texto.append(data)

    @property
    def texto(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._texto)).strip().lower()


def _cargar_jsonld(bloque: str) -> list:
    """Convierte un bloque JSON-LD en objetos Python; ignora bloques defectuosos."""
    texto = re.sub(r"^\s*<!--|-->\s*$", "", bloque.strip())
    try:
        datos = json.loads(texto, strict=False) if texto else []
    except json.JSONDecodeError:
        return []
    return datos if isinstance(datos, list) else [datos]


def _tipos(nodo: dict) -> set[str]:
    tipo = nodo.get("@type", "")
    return {re.split(r"[/:]", str(t))[-1].lower() for t in (tipo if isinstance(tipo, list) else [tipo])}


def _productos(datos: list) -> list[dict]:
    """Busca nodos Product en la estructura JSON-LD (incluye @graph y variantes)."""
    encontrados, pendientes = [], list(datos)
    while pendientes:
        nodo = pendientes.pop(0)
        if isinstance(nodo, list):
            pendientes.extend(nodo)
        elif isinstance(nodo, dict):
            if _tipos(nodo) & TIPOS_PRODUCTO:
                encontrados.append(nodo)
            pendientes.extend(nodo[c] for c in ("@graph", "mainEntity", "hasVariant") if c in nodo)
    return encontrados


def _ofertas(producto: dict) -> list[dict]:
    """Devuelve las ofertas de un producto; abre las AggregateOffer si traen el detalle."""
    bruto = producto.get("offers")
    resultado = []
    for oferta in bruto if isinstance(bruto, list) else [bruto]:
        if not isinstance(oferta, dict):
            continue
        internas = oferta.get("offers")
        internas = internas if isinstance(internas, list) else [internas]
        internas = [o for o in internas if isinstance(o, dict)]
        resultado.extend(internas or [oferta])
    return resultado


def _estado(valor) -> str | None:
    if isinstance(valor, list):
        valor = valor[0] if valor else None
    if not valor:
        return None
    return ESTADOS_SCHEMA.get(re.split(r"[/:#]", str(valor).strip())[-1].lower().replace(" ", ""))


def _precio(oferta: dict) -> float | None:
    for clave in ("price", "lowPrice"):
        if (precio := a_euros(oferta.get(clave))) is not None:
            return precio
    spec = oferta.get("priceSpecification")
    for s in spec if isinstance(spec, list) else [spec]:
        if isinstance(s, dict) and (precio := a_euros(s.get("price"))) is not None:
            return precio
    return None


def _vendedor(oferta: dict) -> str | None:
    for clave in ("seller", "offeredBy"):
        v = oferta.get(clave)
        v = v[0] if isinstance(v, list) and v else v
        if isinstance(v, dict) and v.get("name"):
            return str(v["name"]).strip()
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _aplicar_ofertas(lectura: Lectura, ofertas: list[_Oferta], ajustes: dict) -> None:
    """Elige la oferta más relevante: primero una compra válida, luego la más informativa."""
    maximo, permitir = float(ajustes["precio_maximo"]), bool(ajustes["permitir_marketplace"])

    def prioridad(o: _Oferta) -> tuple:
        comprable = o.estado in COMPRABLES
        en_precio = o.precio is None or o.precio <= maximo
        if comprable and en_precio and (permitir or not o.externo):
            rango = 0
        elif comprable:
            rango = 1 if not o.externo else 2
        else:
            rango = {"solo_tienda": 3, "agotado": 4}.get(o.estado or "", 5)
        return rango, o.precio if o.precio is not None else float("inf")

    elegida = min(ofertas, key=prioridad)
    lectura.estado = elegida.estado or "desconocido"
    lectura.precio, lectura.vendedor, lectura.vendedor_externo = elegida.precio, elegida.vendedor, elegida.externo


def analizar_html(html_texto: str, tienda: dict, ajustes: dict) -> Lectura:
    """Analiza el HTML de una ficha ya descargada (función pura, sin red)."""
    lectura = Lectura(tienda=tienda["nombre"], url=tienda["url"])
    extractor = _ExtractorHTML()
    extractor.feed(html_texto)
    extractor.close()
    propios = [normalizar(v) for v in tienda.get("vendedores_propios", [tienda["nombre"]])]
    palabra = normalizar(tienda.get("palabra_clave", ajustes.get("palabra_clave", "")))

    # 1) JSON-LD
    productos = [p for b in extractor.bloques_jsonld for p in _productos(_cargar_jsonld(b)) if p.get("offers")]
    productos = [p for p in productos if palabra in normalizar(p.get("name", ""))] or productos
    precio_previo = None
    if productos:
        ofertas = []
        for o in _ofertas(productos[0]):
            vendedor = _vendedor(o)
            externo = bool(vendedor) and not any(p and p in normalizar(vendedor) for p in propios)
            ofertas.append(_Oferta(_estado(o.get("availability")), _precio(o), vendedor, externo))
        if ofertas:
            _aplicar_ofertas(lectura, ofertas, ajustes)
            if lectura.estado != "desconocido":
                lectura.fuente = "json-ld"
                return lectura
            precio_previo = lectura.precio  # hay precio pero no disponibilidad: seguimos buscando

    # 2) Microdatos y metaetiquetas
    precio_md = next((p for v in extractor.microdatos.get("price", []) if (p := a_euros(v)) is not None), None)
    precio_meta = a_euros(extractor.meta.get("product:price:amount") or extractor.meta.get("og:price:amount"))
    precio = next((p for p in (precio_previo, precio_md, precio_meta) if p is not None), None)
    estado_md = next((e for v in extractor.microdatos.get("availability", []) if (e := _estado(v))), None)
    disponibilidad_meta = extractor.meta.get("product:availability") or extractor.meta.get("og:availability") or ""
    estado_meta = ESTADOS_META.get(disponibilidad_meta.strip().lower())
    if estado_md or estado_meta:
        lectura.estado, lectura.precio = estado_md or estado_meta, precio
        lectura.fuente = "microdatos" if estado_md else "meta"
        return lectura

    # 3) Páginas de bloqueo: mejor "no verificable" que una conclusión falsa
    texto = extractor.texto
    if any(s in f"{extractor.titulo.lower()} {texto[:3000]}" for s in SENALES_ANTIBOT):
        lectura.detalle = "bloqueada por la protección anti-bots de la web"
        return lectura

    # 4) Frases visibles (último recurso)
    ok = [f for f in tienda.get("texto_disponible", ajustes.get("texto_disponible", [])) if f.lower() in texto]
    ko = [f for f in tienda.get("texto_agotado", ajustes.get("texto_agotado", [])) if f.lower() in texto]
    lectura.precio = precio
    if ok and not ko:
        lectura.estado, lectura.fuente = "disponible", "texto"
    elif ko and not ok:
        lectura.estado, lectura.fuente = "agotado", "texto"
    elif ok and ko:
        lectura.detalle = "señales contradictorias en la página"
    elif len(texto) < 200:
        lectura.detalle = "página casi vacía (la tienda carga los datos con JavaScript)"
    else:
        lectura.detalle = "sin datos de disponibilidad legibles"
    return lectura


def revisar(tienda: dict, ajustes: dict) -> Lectura:
    """Descarga y analiza una ficha. No lanza excepciones: los fallos quedan en la lectura."""
    lectura = Lectura(tienda=tienda["nombre"], url=tienda["url"])
    try:
        respuesta = http.get(
            tienda["url"],
            impersonate="chrome",  # huella de navegador real para no ser descartado como bot
            headers={"Accept-Language": "es-ES,es;q=0.9"},
            timeout=TIMEOUT_SEGUNDOS,
            allow_redirects=True,
        )
    except Exception as error:  # red caída, DNS, timeout, TLS...
        lectura.detalle = f"sin respuesta ({type(error).__name__})"
        return lectura

    host = (urlparse(str(respuesta.url)).hostname or "").lower()
    if host.endswith("queue-it.net") or "queue" in host.split(".")[0]:
        lectura.estado, lectura.fuente = "cola", "url"
        lectura.detalle = "cola virtual activa: suele indicar reposición"
        return lectura
    if respuesta.status_code != 200:
        bloqueo = respuesta.status_code in (401, 403, 429, 503)
        lectura.detalle = f"HTTP {respuesta.status_code}" + (" (probable bloqueo anti-bots)" if bloqueo else "")
        return lectura

    contenido = respuesta.content[:MAX_BYTES_PAGINA]
    try:
        html_texto = contenido.decode(respuesta.encoding or "utf-8", errors="replace")
    except LookupError:  # codificación desconocida declarada por la web
        html_texto = contenido.decode("utf-8", errors="replace")
    try:
        return analizar_html(html_texto, tienda, ajustes)
    except Exception as error:  # HTML muy roto: se informa en vez de abortar la ronda
        lectura.detalle = f"no se pudo analizar la página ({type(error).__name__})"
        return lectura

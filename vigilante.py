"""
Vigilante de stock de la PS5 Pro a precio oficial (pensado para GitHub Actions).

En cada ejecución revisa las tiendas de config.toml y avisa por Telegram de:
  - stock nuevo (en stock o en reserva a precio <= máximo): aviso destacado y CON sonido,
  - stock que desaparece: aviso breve y silencioso, para no ir a buscarla en balde,
  - un resumen diario silencioso con el estado de cada tienda (también en ejecuciones manuales).
Con ORIGEN=simulacro solo envía un ejemplo del aviso de stock, sin revisar tiendas.
Las ejecuciones lanzadas desde fuera (cron-job.org) llegan con ORIGEN=cron y cuentan como
programadas. El estado entre ejecuciones se guarda en state.json (caché de GitHub).
Los datos de Telegram y del proxy llegan por variables de entorno (secretos), nunca en el código.
"""

from __future__ import annotations

import html
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from lector_fichas import Lectura, revisar

CARPETA = Path(__file__).resolve().parent
RUTA_CONFIG = CARPETA / "config.toml"
RUTA_ESTADO = CARPETA / "state.json"
ZONA = ZoneInfo("Europe/Madrid")
LIMITE_TELEGRAM = 4000  # Telegram admite 4096 caracteres por mensaje
DESCRIPCION = {
    "disponible": "en stock", "reserva": "en reserva", "cola": "cola virtual activa",
    "solo_tienda": "solo en tienda física", "agotado": "agotado", "desconocido": "no verificable",
}


def cargar_config() -> dict:
    """Lee config.toml, aplica valores por defecto y valida las tiendas activas."""
    with RUTA_CONFIG.open("rb") as fichero:
        config = tomllib.load(fichero)
    for clave, defecto in (("precio_maximo", 900.0), ("precio_minimo_logico", 600.0),
                           ("permitir_marketplace", False), ("hora_resumen_diario", 9)):
        config.setdefault(clave, defecto)
    tiendas = [t for t in config.get("tiendas", []) if t.get("activa", True) and t.get("url")]
    nombres = set()
    for tienda in tiendas:
        if not str(tienda["url"]).startswith("https://"):
            raise ValueError(f"la URL de {tienda.get('nombre', '?')} debe empezar por https://")
        tienda.setdefault("nombre", tienda["url"])
        if tienda["nombre"] in nombres:
            raise ValueError(f"hay dos tiendas llamadas '{tienda['nombre']}': cada nombre debe ser único")
        nombres.add(tienda["nombre"])
    if not tiendas:
        raise ValueError("no hay ninguna tienda activa con URL")
    config["tiendas"] = tiendas
    config["maximos"] = {t["nombre"]: float(t.get("precio_maximo", config["precio_maximo"])) for t in tiendas}
    config["avisos"] = {t["nombre"]: t["aviso"] for t in tiendas if t.get("aviso")}
    return config


def cargar_estado() -> dict:
    """Recupera lo que se avisó en la ejecución anterior (vacío la primera vez)."""
    if not RUTA_ESTADO.exists():
        return {}
    try:
        return json.loads(RUTA_ESTADO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Aviso: state.json ilegible ({error}); se empieza de cero.")
        return {}


def maximo_de(nombre: str, config: dict) -> float:
    return config.get("maximos", {}).get(nombre, float(config["precio_maximo"]))


def es_oportunidad(lectura: Lectura, config: dict) -> bool:
    if lectura.estado == "cola":
        return True
    if lectura.estado not in ("disponible", "reserva"):
        return False
    if lectura.vendedor_externo and not config["permitir_marketplace"]:
        return False
    return lectura.precio is None or lectura.precio <= maximo_de(lectura.tienda, config)


def euros(valor: float | None) -> str:
    if valor is None:
        return "precio no detectado"
    entero, decimales = f"{valor:,.2f}".split(".")
    return f"{entero.replace(',', '.')},{decimales} €"


def minutos_desde(marca: str | None, ahora: datetime) -> int | None:
    """Minutos transcurridos desde una marca ISO guardada en el estado (None si no hay)."""
    if not marca:
        return None
    try:
        return max(0, round((ahora - datetime.fromisoformat(marca)).total_seconds() / 60))
    except (TypeError, ValueError):
        return None


def bloque_alerta(lectura: Lectura, config: dict) -> str:
    """Bloque de una tienda dentro del aviso de stock."""
    e = html.escape
    nombre, enlace = e(lectura.tienda.upper()), e(lectura.url, quote=True)
    if lectura.estado == "cola":
        lineas = [f"🟡 <b>{nombre}</b>", "⏳ Cola virtual activa: suele indicar reposición",
                  f'👉 <a href="{enlace}"><b>ENTRAR EN LA COLA</b></a>']
    else:
        estado = "EN STOCK" if lectura.estado == "disponible" else "EN RESERVA"
        lineas = [f"✅ <b>{nombre}</b>", f"💶 <b>{e(euros(lectura.precio))}</b> · {estado}",
                  f'👉 <a href="{enlace}"><b>COMPRAR AHORA</b></a>']
    avisos = []
    if lectura.precio is not None and lectura.precio < float(config["precio_minimo_logico"]):
        avisos.append("precio anormalmente bajo: comprueba que sea la consola")
    if lectura.precio is None and lectura.estado != "cola":
        avisos.append("mira el precio en la web")
    if lectura.vendedor_externo:
        avisos.append(f"vendedor externo: {lectura.vendedor}")
    if lectura.fuente == "texto":
        avisos.append("detectado por el texto de la página, confírmalo")
    if lectura.tienda in config.get("avisos", {}):
        avisos.append(config["avisos"][lectura.tienda])
    lineas += [f"⚠️ {e(a)}" for a in avisos]
    return "\n".join(lineas)


def mensaje_alerta(lecturas: list[Lectura], config: dict, ahora: datetime, simulacro: bool = False) -> str:
    """Aviso de stock: el único mensaje con sonido, diseñado para verse de un vistazo."""
    cabecera = "🟢🟢🟢 <b>¡HAY STOCK DE PS5 PRO!</b> 🟢🟢🟢\n🚨 <b>CORRE: suele agotarse en minutos</b> 🚨"
    if simulacro:
        cabecera = "🧪 <b>SIMULACRO</b> · así llegará el aviso real\n\n" + cabecera
    cuerpo = "\n\n".join(bloque_alerta(lec, config) for lec in lecturas)
    return f"{cabecera}\n\n{cuerpo}\n\n⏱️ Detectado a las {ahora:%H:%M}"


def mensaje_fin(nombres: list[str], previas: dict, ahora: datetime) -> str:
    """Aviso silencioso de que el stock avisado ya no está."""
    lineas = []
    for nombre in nombres:
        minutos = minutos_desde(previas.get(nombre, {}).get("desde"), ahora)
        hace = f" (lo detecté hace {minutos} min)" if minutos is not None else ""
        lineas.append(f"🔴 <b>{html.escape(nombre)}</b>: se acabó{hace}")
    return "\n".join(lineas)


def linea_resumen(lectura: Lectura, config: dict) -> str:
    """Una línea por tienda para el resumen diario o la prueba manual."""
    comprable = lectura.estado in ("disponible", "reserva")
    if comprable and lectura.vendedor_externo:
        partes = ["solo marketplace" + (f" ({lectura.vendedor})" if lectura.vendedor else "")]
    else:
        partes = [DESCRIPCION.get(lectura.estado, lectura.estado)]
    if lectura.precio is not None:
        partes.append(euros(lectura.precio))
    if comprable and lectura.precio is not None and lectura.precio > maximo_de(lectura.tienda, config):
        partes.append("por encima del máximo")
    if lectura.detalle:
        partes.append(lectura.detalle)
    marca = "✅" if es_oportunidad(lectura, config) else ("❔" if lectura.estado == "desconocido" else "▫️")
    return f"{marca} <b>{html.escape(lectura.tienda)}</b>: {html.escape(' · '.join(partes))}"


def enviar_telegram(texto: str, silencioso: bool = False) -> None:
    """Envía un mensaje al chat configurado. Lanza RuntimeError si Telegram no lo acepta."""
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    if len(texto) > LIMITE_TELEGRAM:  # se corta por línea para no romper etiquetas HTML
        texto = texto[: texto.rfind("\n", 0, LIMITE_TELEGRAM)] + "\n…"
    cuerpo = json.dumps({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(),
        "text": texto,
        "parse_mode": "HTML",
        "disable_notification": silencioso,
        "link_preview_options": {"is_disabled": True},
    }).encode("utf-8")
    peticion = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=cuerpo, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=20) as respuesta:
            respuesta.read()
    except urllib.error.HTTPError as error:  # sin la URL, para no exponer el token en los logs
        raise RuntimeError(f"Telegram rechazó el mensaje (HTTP {error.code}): {error.read()[:200]!r}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"no se pudo contactar con Telegram ({error.reason})") from None


def main(ahora: datetime | None = None) -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("ERROR: faltan los secretos TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID en GitHub.")
        return 1
    try:
        config = cargar_config()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"ERROR en config.toml: {error}")
        return 1

    ahora = ahora or datetime.now(ZONA)
    evento, origen = os.environ.get("GITHUB_EVENT_NAME", "local"), os.environ.get("ORIGEN") or "manual"
    print(f"Evento: {evento} · origen: {origen if evento == 'workflow_dispatch' else '-'}")

    if evento == "workflow_dispatch" and origen == "simulacro":  # solo muestra cómo llega el aviso
        tienda = config["tiendas"][0]
        ejemplo = Lectura(tienda=tienda["nombre"], url=tienda["url"], estado="disponible", precio=899.99)
        try:
            enviar_telegram(mensaje_alerta([ejemplo], config, ahora, simulacro=True))
        except RuntimeError as error:
            print(f"ERROR al enviar el simulacro: {error}")
            return 1
        print("Simulacro enviado: no se han revisado tiendas ni tocado el estado.")
        return 0

    manual = evento == "workflow_dispatch" and origen != "cron"
    previo = cargar_estado()
    previas: dict = previo.get("oportunidades", {})

    lecturas = [revisar(tienda, config) for tienda in config["tiendas"]]
    for lec in lecturas:  # registro visible en la pestaña Actions
        print(f"{lec.tienda:<24} {lec.estado:<12} {euros(lec.precio):<20} {lec.fuente:<10} {lec.detalle}")

    actuales = {lec.tienda: lec for lec in lecturas if es_oportunidad(lec, config)}
    mensajes: list[tuple[str, bool]] = []  # (texto, silencioso)
    nuevas = [lec for nombre, lec in actuales.items() if previas.get(nombre, {}).get("precio", "-") != lec.precio]
    if nuevas:
        mensajes.append((mensaje_alerta(nuevas, config, ahora), False))
    terminadas = [nombre for nombre in previas if nombre not in actuales]
    if terminadas:
        mensajes.append((mensaje_fin(terminadas, previas, ahora), True))

    hoy = ahora.date().isoformat()
    hora_resumen = int(config["hora_resumen_diario"])
    toca_resumen = manual or (0 <= hora_resumen <= ahora.hour and previo.get("ultimo_resumen") != hoy)
    if toca_resumen:
        titulo = "🧪 Prueba manual" if manual else "📋 Resumen diario"
        cabecera = f"{titulo} · PS5 Pro (máx. {euros(float(config['precio_maximo']))})"
        mensajes.append((cabecera + "\n" + "\n".join(linea_resumen(lec, config) for lec in lecturas), True))

    errores = 0
    for texto, silencioso in mensajes:
        try:
            enviar_telegram(texto, silencioso=silencioso)
        except RuntimeError as error:
            print(f"ERROR al avisar por Telegram: {error}")
            errores += 1

    estado = {
        "oportunidades": {
            nombre: {
                "precio": lec.precio,
                "desde": previas.get(nombre, {}).get("desde", ahora.isoformat(timespec="minutes")),
            }
            for nombre, lec in actuales.items()
        },
        "ultimo_resumen": hoy if toca_resumen and not manual else previo.get("ultimo_resumen"),
        "ultima_ejecucion": ahora.isoformat(timespec="seconds"),
    }
    if errores:  # si algún aviso no salió, se conserva lo anterior para reintentarlo en la próxima ronda
        estado["oportunidades"], estado["ultimo_resumen"] = previas, previo.get("ultimo_resumen")
    try:
        RUTA_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        print(f"Aviso: no se pudo guardar state.json ({error})")
    print(f"Mensajes enviados: {len(mensajes) - errores} de {len(mensajes)}")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())

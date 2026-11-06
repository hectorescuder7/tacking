"""
Vigilante de stock de la PS5 Pro a precio oficial (pensado para GitHub Actions).

En cada ejecución revisa las tiendas de config.toml y avisa por Telegram de:
  - oportunidades nuevas (en stock o en reserva a precio <= máximo, vendida por la tienda),
  - oportunidades que desaparecen, para no ir a buscarla en balde,
  - un resumen diario con el estado de cada tienda (y siempre en las ejecuciones manuales).
El estado entre ejecuciones se guarda en state.json, que GitHub conserva en su caché.
Los datos de Telegram llegan por variables de entorno (secretos de GitHub), nunca en el código.
Las ejecuciones lanzadas desde fuera (cron-job.org) llegan con ORIGEN=cron y cuentan como
programadas: no generan el resumen de prueba de las ejecuciones manuales.
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
    for tienda in tiendas:
        if not str(tienda["url"]).startswith("https://"):
            raise ValueError(f"la URL de {tienda.get('nombre', '?')} debe empezar por https://")
        tienda.setdefault("nombre", tienda["url"])
    if not tiendas:
        raise ValueError("no hay ninguna tienda activa con URL")
    config["tiendas"] = tiendas
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


def es_oportunidad(lectura: Lectura, config: dict) -> bool:
    if lectura.estado == "cola":
        return True
    if lectura.estado not in ("disponible", "reserva"):
        return False
    if lectura.vendedor_externo and not config["permitir_marketplace"]:
        return False
    return lectura.precio is None or lectura.precio <= float(config["precio_maximo"])


def euros(valor: float | None) -> str:
    if valor is None:
        return "precio no detectado"
    entero, decimales = f"{valor:,.2f}".split(".")
    return f"{entero.replace(',', '.')},{decimales} €"


def bloque_alerta(lectura: Lectura, config: dict) -> str:
    """Texto de aviso para una tienda con oportunidad."""
    e = html.escape
    if lectura.estado == "cola":
        cuerpo = "cola virtual activa: suele indicar reposición, entra ya"
    else:
        cuerpo = f"{euros(lectura.precio)} · {DESCRIPCION[lectura.estado]}"
    avisos = []
    if lectura.precio is not None and lectura.precio < float(config["precio_minimo_logico"]):
        avisos.append("precio anormalmente bajo: comprueba que sea la consola")
    if lectura.precio is None and lectura.estado != "cola":
        avisos.append("mira el precio en la web")
    if lectura.vendedor_externo:
        avisos.append(f"vendedor externo: {lectura.vendedor}")
    if lectura.fuente == "texto":
        avisos.append("detectado por el texto de la página, confírmalo")
    lineas = [f"<b>{e(lectura.tienda)}</b>: {e(cuerpo)}"]
    if avisos:
        lineas.append("⚠️ " + e("; ".join(avisos)))
    lineas.append(f'<a href="{e(lectura.url, quote=True)}">Abrir ficha</a>')
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
    if comprable and lectura.precio is not None and lectura.precio > float(config["precio_maximo"]):
        partes.append("por encima del máximo")
    if lectura.detalle:
        partes.append(lectura.detalle)
    marca = "✅" if es_oportunidad(lectura, config) else ("❔" if lectura.estado == "desconocido" else "▫️")
    return f"{marca} <b>{html.escape(lectura.tienda)}</b>: {html.escape(' · '.join(partes))}"


def enviar_telegram(texto: str) -> None:
    """Envía un mensaje al chat configurado. Lanza RuntimeError si Telegram no lo acepta."""
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    if len(texto) > LIMITE_TELEGRAM:  # se corta por línea para no romper etiquetas HTML
        texto = texto[: texto.rfind("\n", 0, LIMITE_TELEGRAM)] + "\n…"
    cuerpo = json.dumps({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(),
        "text": texto,
        "parse_mode": "HTML",
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
    manual = evento == "workflow_dispatch" and origen != "cron"
    print(f"Evento: {evento} · origen: {origen if evento == 'workflow_dispatch' else '-'}")
    previo = cargar_estado()
    previas: dict = previo.get("oportunidades", {})

    lecturas = [revisar(tienda, config) for tienda in config["tiendas"]]
    for lec in lecturas:  # registro visible en la pestaña Actions
        print(f"{lec.tienda:<20} {lec.estado:<12} {euros(lec.precio):<20} {lec.fuente:<10} {lec.detalle}")

    actuales = {lec.tienda: lec for lec in lecturas if es_oportunidad(lec, config)}
    mensajes = []
    nuevas = [lec for nombre, lec in actuales.items() if previas.get(nombre, {}).get("precio", "-") != lec.precio]
    if nuevas:
        bloques = "\n\n".join(bloque_alerta(lec, config) for lec in nuevas)
        mensajes.append("🚨 <b>PS5 Pro a precio oficial</b>\n\n" + bloques)
    terminadas = [nombre for nombre in previas if nombre not in actuales]
    if terminadas:
        mensajes.append("ℹ️ Ya no aparece a precio oficial en: " + html.escape(", ".join(terminadas)))

    hoy = ahora.date().isoformat()
    hora_resumen = int(config["hora_resumen_diario"])
    toca_resumen = manual or (0 <= hora_resumen <= ahora.hour and previo.get("ultimo_resumen") != hoy)
    if toca_resumen:
        titulo = "🧪 Prueba manual" if manual else "📋 Resumen diario"
        cabecera = f"{titulo} · PS5 Pro (máx. {euros(float(config['precio_maximo']))})"
        mensajes.append(cabecera + "\n" + "\n".join(linea_resumen(lec, config) for lec in lecturas))

    errores = 0
    for mensaje in mensajes:
        try:
            enviar_telegram(mensaje)
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

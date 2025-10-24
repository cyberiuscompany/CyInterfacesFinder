#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CyInterfacesFinder.py
Versión actualizada — robusta extracción de bindings devueltos por IObjectExporter.ServerAlive2().

Características principales:
- Muestra help si se ejecuta sin parámetros.
- Conecta al servicio RPC del target (ncacn_ip_tcp:<target>).
- Llama a IObjectExporter.ServerAlive2() y extrae bindings intentando:
    * claves dict str/bytes
    * indexación b['aNetworkAddr']
    * método get() si existe
    * atributo aNetworkAddr
    * decodificación de bytes/bytearray
    * como último recurso, intenta analizar repr/str y extraer IPs
- Añade verbose que muestra type() y dir() de las entradas problemáticas para ayudar a depurar.
- Guarda CSV opcional con -o/--output
- Requiere impacket
"""
from __future__ import annotations

import sys
import argparse
import socket
import re
import ipaddress
import csv
import traceback
from typing import Any, Dict, List, Optional, Set

try:
    from impacket.dcerpc.v5 import transport
    from impacket.dcerpc.v5.rpcrt import RPC_C_AUTHN_LEVEL_NONE
    from impacket.dcerpc.v5.dcomrt import IObjectExporter
except Exception:
    sys.stderr.write("ERROR: impacket no está disponible. Instálalo con: pip install impacket\n")
    sys.exit(1)


BINDING_RE = re.compile(r"(?P<prot>[^:]+):(?P<addr>[^\[]+)(\[(?P<bracket>.*)\])?")
IP_RE = re.compile(r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})")
PORT_RE = re.compile(r"[:\[](?P<port>\d{1,5})\]")


def parse_network_addr(aNetworkAddr: str) -> Dict[str, Optional[str]]:
    result = {'raw': aNetworkAddr, 'protocol': None, 'addr_raw': None, 'ip': None, 'port': None}
    if not aNetworkAddr:
        return result
    m = BINDING_RE.match(aNetworkAddr)
    if m:
        result['protocol'] = m.group('prot')
        result['addr_raw'] = m.group('addr').strip()
        mip = IP_RE.search(result['addr_raw'])
        if mip:
            result['ip'] = mip.group('ip')
        mport = PORT_RE.search(aNetworkAddr)
        if mport:
            result['port'] = mport.group('port')
        else:
            if ':' in result['addr_raw']:
                try:
                    hostpart = result['addr_raw'].rsplit(':', 1)[-1]
                    if hostpart.isdigit():
                        result['port'] = hostpart
                except Exception:
                    pass
    else:
        result['addr_raw'] = aNetworkAddr
        mip = IP_RE.search(aNetworkAddr)
        if mip:
            result['ip'] = mip.group('ip')
        mport = PORT_RE.search(aNetworkAddr)
        if mport:
            result['port'] = mport.group('port')
    return result


def resolve_name(addr_raw: str) -> Optional[str]:
    if not addr_raw:
        return None
    try:
        mip = IP_RE.search(addr_raw)
        if mip:
            return mip.group('ip')
        host = addr_raw.split(':')[0]
        host = host.strip('\\/')
        return socket.gethostbyname(host)
    except Exception:
        return None


def infer_network(ip_str: Optional[str]) -> Optional[str]:
    if not ip_str:
        return None
    try:
        ip = ipaddress.IPv4Address(ip_str)
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        return str(net)
    except Exception:
        return None


def safe_decode(val: Any) -> str:
    try:
        if isinstance(val, (bytes, bytearray)):
            return val.decode('utf-8', errors='replace')
        return str(val)
    except Exception:
        try:
            return repr(val)
        except Exception:
            return "<unreadable>"


def extract_binding_text(b: Any, verbose: bool = False) -> str:
    """
    Intentos (ordenados) para recuperar texto representativo del binding:
      1) Si es dict: claves 'aNetworkAddr' (str o bytes), o primer value plausible.
      2) Intentar indexing: b['aNetworkAddr']
      3) Intentar método get: b.get('aNetworkAddr')
      4) Intentar atributo: getattr(b, 'aNetworkAddr')
      5) Si es bytes/bytearray: decode utf-8 errors='replace'
      6) str()/repr() y extracción de IP si posible
    """
    aNetworkAddr = None

    # 1) Si es dict, intentar claves str y bytes
    try:
        if isinstance(b, dict):
            if 'aNetworkAddr' in b:
                aNetworkAddr = b.get('aNetworkAddr')
            elif b'aNetworkAddr' in b:
                aNetworkAddr = b.get(b'aNetworkAddr')
            else:
                # Tomar primer value que parezca string/bytes
                for k, v in b.items():
                    if isinstance(v, (str, bytes, bytearray)):
                        aNetworkAddr = v
                        break
            # si es lista/tuple, tomar primer elemento
            if isinstance(aNetworkAddr, (list, tuple)) and aNetworkAddr:
                aNetworkAddr = aNetworkAddr[0]
    except Exception:
        aNetworkAddr = None

    # 2) Intentar indexación (muchas estructuras retornadas por impacket permiten __getitem__)
    if aNetworkAddr is None:
        try:
            try:
                aNetworkAddr = b['aNetworkAddr']
            except Exception:
                # no indexable o sin key
                aNetworkAddr = None
        except Exception:
            aNetworkAddr = None

    # 3) Intentar método get()
    if aNetworkAddr is None:
        try:
            get_method = getattr(b, 'get', None)
            if callable(get_method):
                # probar con str key y bytes key
                aNetworkAddr = get_method('aNetworkAddr', None)
                if aNetworkAddr is None:
                    aNetworkAddr = get_method(b'aNetworkAddr', None)
        except Exception:
            aNetworkAddr = None

    # 4) Intentar atributo
    if aNetworkAddr is None:
        try:
            if hasattr(b, 'aNetworkAddr'):
                aNetworkAddr = getattr(b, 'aNetworkAddr')
        except Exception:
            aNetworkAddr = None

    # 5) Si sigue None, intentar tratar b entero (bytes/bytearray o str())
    if aNetworkAddr is None:
        try:
            if isinstance(b, (bytes, bytearray)):
                aNetworkAddr = b.decode('utf-8', errors='replace')
            else:
                s = str(b)
                # algunos objetos podrían devolver bytes en __str__
                if isinstance(s, (bytes, bytearray)):
                    aNetworkAddr = s.decode('utf-8', errors='replace')
                else:
                    aNetworkAddr = s
        except Exception:
            try:
                aNetworkAddr = repr(b)
            except Exception:
                aNetworkAddr = "<unreadable-binding>"

    # 6) Si el valor obtenido es bytes, decodificar
    if isinstance(aNetworkAddr, (bytes, bytearray)):
        try:
            aNetworkAddr = aNetworkAddr.decode('utf-8', errors='replace')
        except Exception:
            aNetworkAddr = safe_decode(aNetworkAddr)

    # Si no logramos obtener una dirección legible, en verbose mostramos info de depuración
    if (not aNetworkAddr or aNetworkAddr.strip() == "" or IP_RE.search(str(aNetworkAddr)) is None) and verbose:
        try:
            print("[DEBUG] Entrada binding problemática:")
            print("  type:", type(b))
            try:
                print("  dir():", dir(b))
            except Exception:
                print("  dir() no disponible")
            try:
                # si es indexable, mostrar keys disponibles (intento prudente)
                if isinstance(b, dict):
                    print("  dict.keys():", list(b.keys()))
                else:
                    # intentar acceder mediante atributos comunes
                    attrs = []
                    for attr in ('aNetworkAddr', 'pNetworkAddress', 'szNetworkAddress', '_fields_', '__dict__'):
                        if hasattr(b, attr):
                            attrs.append(attr)
                    print("  posibles atributos detectados:", attrs)
            except Exception:
                pass
            print("  repr():", repr(b)[:400])
        except Exception:
            pass

    return aNetworkAddr or "<empty>"


def pretty_print_table(rows: List[List[str]], headers: List[str]) -> None:
    if not rows:
        print("[!] tabla vacía.")
        return
    col_widths = [max(len(str(row[i])) for row in rows + [headers]) for i in range(len(headers))]
    sep = '  '
    header_line = sep.join(headers[i].ljust(col_widths[i]) for i in range(len(headers)))
    print(header_line)
    print('-' * (sum(col_widths) + len(sep) * (len(headers) - 1)))
    for r in rows:
        print(sep.join(str(r[i]).ljust(col_widths[i]) for i in range(len(headers))))


def save_csv(path: str, headers: List[str], rows: List[List[Any]]) -> None:
    try:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in rows:
                writer.writerow(r)
        print(f"[+] Guardado CSV en: {path}")
    except Exception as e:
        print(f"[!] Error guardando CSV en {path}: {e}")


def analyze_bindings(bindings: List[Any], verbose: bool = False) -> Dict[str, Any]:
    parsed = []
    networks: Set[str] = set()
    ips_seen: Set[str] = set()
    rows_for_csv: List[List[str]] = []

    for idx, b in enumerate(bindings):
        try:
            aNetworkAddr = extract_binding_text(b, verbose=verbose)
        except Exception:
            aNetworkAddr = safe_decode(b)

        # Algunos objetos devueltos por impacket pueden requerir que se indexen varias veces
        # para extraer la estructura interna; extract_binding_text intenta varias rutas.
        p = parse_network_addr(aNetworkAddr)
        if p['ip'] is None:
            resolved = resolve_name(p.get('addr_raw') or '')
            p['ip'] = resolved

        if p['ip']:
            ips_seen.add(p['ip'])
            net = infer_network(p['ip'])
            if net:
                networks.add(net)
                p['network'] = net
            else:
                p['network'] = None
        else:
            p['network'] = None

        parsed.append(p)
        rows_for_csv.append([p.get('raw') or '', p.get('protocol') or '', p.get('addr_raw') or '',
                             p.get('ip') or '', p.get('port') or '', p.get('network') or ''])

    return {
        'parsed': parsed,
        'networks': networks,
        'ips_seen': ips_seen,
        'csv_rows': rows_for_csv
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog='CyInterfacesFinder',
                                     description='CyInterfacesFinder: recupera y analiza bindings RPC/DCE para inferir interfaces/networks.')
    parser.add_argument('-t', '--target', help='IP o hostname del target (ej: 192.168.1.10)', required=False)
    parser.add_argument('-T', '--timeout', help='Timeout en segundos para conectar (default 10)', type=int, default=10)
    parser.add_argument('-v', '--verbose', action='store_true', help='Salida más detallada (muestra dir() de objetos problemáticos)')
    parser.add_argument('-o', '--output', help='Guardar resultados en CSV (ruta)', required=False)

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()
    target = args.target
    if not target:
        print("[!] Debes indicar un objetivo con -t/--target.")
        parser.print_help()
        sys.exit(2)

    authLevel = RPC_C_AUTHN_LEVEL_NONE
    stringBinding = fr'ncacn_ip_tcp:{target}'

    try:
        rpctransport = transport.DCERPCTransportFactory(stringBinding)
        try:
            rpctransport.set_connect_timeout(args.timeout)
        except Exception:
            pass
    except Exception as e:
        print(f"[!] Error creando transport para {stringBinding}: {e}")
        if args.verbose:
            traceback.print_exc()
        sys.exit(1)

    dce = None
    try:
        try:
            dce = rpctransport.get_dce_rpc()
        except Exception as e:
            print(f"[!] Error obteniendo objeto DCE-RPC desde transport: {e}")
            if args.verbose:
                traceback.print_exc()
            sys.exit(1)

        try:
            dce.set_auth_level(authLevel)
        except Exception:
            pass

        try:
            dce.connect()
        except Exception as e:
            print(f"[!] Error conectando a {target} ({stringBinding}): {e}")
            if args.verbose:
                traceback.print_exc()
            sys.exit(1)

        try:
            objExporter = IObjectExporter(dce)
        except Exception as e:
            print(f"[!] Error creando IObjectExporter: {e}")
            if args.verbose:
                traceback.print_exc()
            sys.exit(1)

        try:
            bindings = objExporter.ServerAlive2()
        except Exception as e:
            print(f"[!] Error llamando a ServerAlive2 en {target}: {e}")
            if args.verbose:
                traceback.print_exc()
            sys.exit(1)

        # Asegurar que bindings sea iterable/listable
        if not isinstance(bindings, list):
            try:
                bindings = list(bindings)
            except Exception:
                # dejamos tal cual si no se puede convertir
                pass

        print(f"[*] Recuperadas {len(bindings) if hasattr(bindings, '__len__') else '??'} entradas de binding desde {target}\n")

        summary = analyze_bindings(bindings, verbose=args.verbose)
        parsed = summary['parsed']
        networks = summary['networks']
        ips_seen = summary['ips_seen']
        rows_for_csv = summary['csv_rows']

        headers = ['raw_binding', 'proto', 'addr_raw', 'resolved_ip', 'port', 'network(/24)']
        pretty_print_table(rows_for_csv, headers)

        print("\n[+] Resumen:")
        print(f"    Entradas totales: {len(parsed)}")
        print(f"    IPs únicas detectadas: {len(ips_seen)} -> {', '.join(sorted(ips_seen)) if ips_seen else 'ninguna'}")
        print(f"    Redes /24 únicas inferidas: {len(networks)} -> {', '.join(sorted(networks)) if networks else 'ninguna'}\n")

        # Análisis heurístico
        risk_msgs: List[str] = []
        if len(networks) > 1:
            risk_msgs.append("ALTO: el host parece tener interfaces en múltiples subredes (/24 distintas). Podría usarse para pivoting.")
        elif len(networks) == 1:
            target_ip = None
            try:
                target_ip = socket.gethostbyname(target)
            except Exception:
                target_ip = None
            if target_ip and infer_network(target_ip) in networks:
                risk_msgs.append("MEDIO: el host anuncia una red que coincide con la del target.")
            else:
                risk_msgs.append("MEDIO: el host anuncia una red interna; comprobar si difiere de la red de origen.")
        else:
            risk_msgs.append("BAJO: no se detectaron redes internas diferentes (heurística /24). No obstante, puede no ser concluyente.")

        if any((item.get('protocol') and 'ncacn_np' in (item.get('protocol') or '')) for item in parsed):
            risk_msgs.append("Nota: hay bindings tipo Named Pipe (ncacn_np).")

        if args.verbose:
            risk_msgs.append("Verbose: ver salida debug arriba con type()/dir() para objetos problemáticos.")

        print('\n'.join('    - ' + m for m in risk_msgs))

        if args.output:
            save_csv(args.output, headers, rows_for_csv)

    finally:
        try:
            if dce is not None:
                dce.disconnect()
        except Exception:
            pass


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Lector y validador de referencia para ficheros de Bloques Basicos y CFG (bb.json).

Implementa el contrato y lo valida contra ``schema/bb_schema_v1.json`` con un
validador de JSON Schema reducido (solo biblioteca estandar, al estilo de
``utils/profiler_json_reader.py``).

Las dos comprobaciones son complementarias:

* ``--validate``: forma del documento (tipos, campos obligatorios, campos no
  documentados, longitudes) segun el esquema.
* ``--check``: invariantes semanticas del contrato (consistencia de metricas,
  aristas validas sobre nodos existentes, IDs correlativos, etc.).

Uso:
    python3 utils/bb_json_reader.py fichero_bb.json --validate
    python3 utils/bb_json_reader.py fichero_bb.json --check
    python3 utils/bb_json_reader.py fichero_bb.json --summary
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "schema" / "bb_schema_v1.json"


class SchemaError(Exception):
    """Fallo al cargar o interpretar el fichero de esquema."""


# --------------------------------------------------------------------------- #
# Validador JSON Schema (subconjunto biblioteca estandar con soporte de $defs)
# --------------------------------------------------------------------------- #

def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value, type_name):
    found = _type_name(value)
    if type_name == "number":
        return found in ("integer", "number")
    if type_name == "integer":
        return found == "integer"
    return found == type_name


def _resolve_ref(root, ref):
    if not ref.startswith("#/"):
        raise SchemaError(f"solo se soportan $ref locales, no '{ref}'")
    node = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise SchemaError(f"$ref no resoluble: '{ref}'")
        node = node[part]
    return node


def validate_instance(instance, schema, root, path="$"):
    """Valida ``instance`` contra ``schema``. Devuelve la lista de errores."""
    errors = []

    if "$ref" in schema:
        target = _resolve_ref(root, schema["$ref"])
        return validate_instance(instance, target, root, path)

    for sub in schema.get("allOf", []):
        errors += validate_instance(instance, sub, root, path)

    if "anyOf" in schema:
        if not any(not validate_instance(instance, sub, root, path)
                   for sub in schema["anyOf"]):
            errors.append(f"{path}: no cumple ninguna de las opciones de anyOf")

    if "oneOf" in schema:
        matched = sum(1 for sub in schema["oneOf"]
                      if not validate_instance(instance, sub, root, path))
        if matched != 1:
            errors.append(f"{path}: oneOf esperaba 1 coincidencia, hay {matched}")

    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(instance, t) for t in types):
            errors.append(f"{path}: se esperaba {'/'.join(types)}, "
                          f"encontrado {_type_name(instance)}")
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: valor {instance!r} fuera de {schema['enum']}")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: valor {instance!r} != {schema['const']!r}")

    if isinstance(instance, str) and "minLength" in schema:
        if len(instance) < schema["minLength"]:
            errors.append(f"{path}: cadena vacia (minLength {schema['minLength']})")

    if isinstance(instance, str) and "pattern" in schema:
        if re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} no casa con patron {schema['pattern']!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimo {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximo {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: {len(instance)} elementos, minimo {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: {len(instance)} elementos, maximo {schema['maxItems']}")
        if "items" in schema:
            items_schema = schema["items"]
            if isinstance(items_schema, list):
                # Validacion posicional (tuplas)
                for i, item in enumerate(instance):
                    if i < len(items_schema):
                        errors += validate_instance(item, items_schema[i], root, f"{path}[{i}]")
            elif isinstance(items_schema, dict):
                for i, item in enumerate(instance):
                    errors += validate_instance(item, items_schema, root, f"{path}[{i}]")

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: falta el campo obligatorio '{name}'")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, value in instance.items():
            child_path = f"{path}.{name}"
            if name in properties:
                errors += validate_instance(value, properties[name], root, child_path)
            elif extra is False:
                errors.append(f"{child_path}: campo no documentado en el esquema")
            elif isinstance(extra, dict):
                errors += validate_instance(value, extra, root, child_path)

    return errors


def load_schema(schema_path=None):
    path = Path(schema_path) if schema_path else DEFAULT_SCHEMA
    try:
        with open(path, encoding="utf-8") as fh:
            schema = json.load(fh)
    except FileNotFoundError:
        raise SchemaError(f"no existe el esquema: {path}")
    except json.JSONDecodeError as exc:
        raise SchemaError(f"esquema no es JSON valido ({path}): {exc}")
    if not isinstance(schema, dict) or "$defs" not in schema:
        raise SchemaError(f"esquema sin $defs ({path})")
    return schema


def load_document(json_path):
    with open(json_path, encoding="utf-8") as fh:
        return json.load(fh)


def validate_document(doc, schema=None):
    """Errores de forma del documento (lista de mensajes, vacia = valido)."""
    schema = schema if schema is not None else load_schema()
    return validate_instance(doc, schema, schema, "$")


def validate_file(json_path, schema_path=None):
    """Valida un fichero JSON directamente contra el esquema."""
    doc = load_document(json_path)
    schema = load_schema(schema_path)
    return validate_document(doc, schema)


# --------------------------------------------------------------------------- #
# Invariantes semanticas del contrato de Bloques Basicos y CFG
# --------------------------------------------------------------------------- #

def check_invariants(doc):
    """Comprueba las invariantes semanticas del grafo (CFG).

    Devuelve ``(errors, warnings)``.
    """
    errors = []
    warnings = []

    nodes = doc.get("nodes", [])
    edges = doc.get("edges", [])
    summary = doc.get("summary", {})

    # 1. Coherencia del resumen global
    if "total_unique_bbs" in summary and summary["total_unique_bbs"] != len(nodes):
        errors.append(
            f"summary.total_unique_bbs ({summary['total_unique_bbs']}) no coincide con len(nodes) ({len(nodes)})"
        )
    if "total_edges" in summary and summary["total_edges"] != len(edges):
        errors.append(
            f"summary.total_edges ({summary['total_edges']}) no coincide con len(edges) ({len(edges)})"
        )

    # 2. Invariantes por nodo
    node_ids = set()
    for idx, node in enumerate(nodes):
        nid = node.get("id")
        if nid is None:
            continue

        if nid in node_ids:
            errors.append(f"nodes[{idx}]: id duplicado {nid}")
        node_ids.add(nid)

        if nid != idx:
            warnings.append(f"nodes[{idx}]: el id={nid} no coincide con el indice posicional={idx}")

        # Comprobacion de PC
        pc = node.get("pc", {})
        start_str = pc.get("start", "")
        end_str = pc.get("end", "")
        try:
            start_val = int(start_str, 16)
            end_val = int(end_str, 16)
            if start_val > end_val:
                warnings.append(f"nodes[{idx}] (id={nid}): start_pc ({start_str}) > end_pc ({end_str})")
        except (ValueError, TypeError):
            errors.append(f"nodes[{idx}] (id={nid}): formato hexadecimal invalido en PC ({start_str} - {end_str})")

        # Comprobacion de metricas internas
        metrics = node.get("metrics", {})
        insn_meta = metrics.get("instructions", {})
        total = insn_meta.get("total", 0)
        scalar = insn_meta.get("scalar", 0)
        vector = insn_meta.get("vector", 0)
        ratio = insn_meta.get("vector_ratio", 0.0)

        if total != (scalar + vector):
            errors.append(
                f"nodes[{idx}] (id={nid}): total={total} != scalar({scalar}) + vector({vector})"
            )

        if total > 0:
            calc_ratio = vector / total
            if abs(calc_ratio - ratio) > 0.001:
                errors.append(
                    f"nodes[{idx}] (id={nid}): vector_ratio ({ratio:.4f}) no coincide con {vector}/{total} ({calc_ratio:.4f})"
                )

        # Comprobacion de instrucciones si estan presentes
        instructions = node.get("instructions")
        if instructions is not None:
            if len(instructions) != total:
                errors.append(
                    f"nodes[{idx}] (id={nid}): len(instructions) ({len(instructions)}) != metrics.total ({total})"
                )

            for i_idx, insn in enumerate(instructions):
                if isinstance(insn, list) and len(insn) == 4:
                    _asm, uf_id, dst_arr, src_arr = insn
                    if not (0 <= uf_id <= 6):
                        errors.append(f"nodes[{idx}].instructions[{i_idx}]: uf_id {uf_id} fuera de rango [0..6]")
                    for reg in dst_arr:
                        if not (0 <= reg <= 95):
                            errors.append(f"nodes[{idx}].instructions[{i_idx}]: dst reg {reg} fuera de rango [0..95]")
                    for reg in src_arr:
                        if not (0 <= reg <= 95):
                            errors.append(f"nodes[{idx}].instructions[{i_idx}]: src reg {reg} fuera de rango [0..95]")

    # 3. Invariantes de aristas
    seen_edges = set()
    num_nodes = len(nodes)
    for e_idx, edge in enumerate(edges):
        src = edge.get("src")
        dst = edge.get("dst")
        count = edge.get("count", 0)

        if src is None or dst is None:
            continue

        if not (0 <= src < num_nodes):
            errors.append(f"edges[{e_idx}]: src {src} fuera de rango de nodos (0..{num_nodes - 1})")
        if not (0 <= dst < num_nodes):
            errors.append(f"edges[{e_idx}]: dst {dst} fuera de rango de nodos (0..{num_nodes - 1})")

        if count <= 0:
            warnings.append(f"edges[{e_idx}]: count={count} deberia ser estrictamente positivo")

        edge_pair = (src, dst)
        if edge_pair in seen_edges:
            errors.append(f"edges[{e_idx}]: transicion duplicada ({src} -> {dst})")
        seen_edges.add(edge_pair)

    return errors, warnings


def check(json_path, schema_path=None, quiet_warnings=False):
    """Comprueba un fichero JSON contra el esquema y sus invariantes semanticas."""
    doc = load_document(json_path)
    schema = load_schema(schema_path)

    errors = validate_document(doc, schema)
    if errors:
        return False, f"Validation FAILED: {len(errors)} error(es) de esquema"

    inv_errors, inv_warnings = check_invariants(doc)
    if not quiet_warnings:
        for warn in inv_warnings:
            print(f"[WARN] {warn}", file=sys.stderr)

    if inv_errors:
        return False, f"Validation FAILED: {len(inv_errors)} invariante(s) semantica(s) rota(s)"

    nnodes = len(doc.get("nodes", []))
    nedges = len(doc.get("edges", []))
    msg = f"Validation SUCCESS: {nnodes} bloques basicos, {nedges} aristas"
    if inv_warnings:
        msg += f", {len(inv_warnings)} aviso(s)"
    return True, msg


# --------------------------------------------------------------------------- #
# Resumen cuantitativo y estadistico
# --------------------------------------------------------------------------- #

def summarize(doc):
    """Genera un resumen en lineas de texto del grafo de control."""
    summary = doc.get("summary", {})
    nodes = doc.get("nodes", [])
    edges = doc.get("edges", [])

    total_unique_bbs = summary.get("total_unique_bbs", len(nodes))
    total_edges = summary.get("total_edges", len(edges))

    total_execs = 0
    total_dynamic_insns = 0
    total_vector_insns = 0
    vec_bbs_count = 0

    for n in nodes:
        metrics = n.get("metrics", {})
        insns = metrics.get("instructions", {})
        tot = insns.get("total", 0)
        vec = insns.get("vector", 0)
        exec_count = metrics.get("execution_count", n.get("exec_count", 0))

        if vec > 0:
            vec_bbs_count += 1
        total_execs += exec_count
        total_dynamic_insns += tot * exec_count
        total_vector_insns += vec * exec_count

    vec_ratio = (total_vector_insns / total_dynamic_insns * 100.0) if total_dynamic_insns > 0 else 0.0
    vec_bb_ratio = (vec_bbs_count / total_unique_bbs * 100.0) if total_unique_bbs > 0 else 0.0

    lines = [
        "=" * 70,
        " RESUMEN DEL GRAFO DE CONTROL (CFG) - RAVE",
        "=" * 70,
        f"  Generador:                  {doc.get('generator', 'N/A')}",
        f"  Version del esquema:        {doc.get('schema_version', 'N/A')}",
        f"  Binario analizado:          {doc.get('binary', 'N/A') or 'null'}",
        f"  Bloques basicos unicos:     {total_unique_bbs:,}",
        f"  Aristas (transiciones):     {total_edges:,}",
        f"  Bloques con codigo vector:  {vec_bbs_count:,} ({vec_bb_ratio:.1f}%)",
        f"  Ejecuciones totales BBs:    {total_execs:,}",
        f"  Instrucciones dinamicas:    {total_dynamic_insns:,}",
        f"  Instrucciones vectoriales:  {total_vector_insns:,} ({vec_ratio:.2f}%)",
        "=" * 70,
    ]
    return lines


# --------------------------------------------------------------------------- #
# CLI Principal
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(
        description="Reference reader and validator for RAVE Basic Block and CFG JSON."
    )
    parser.add_argument("json_file", help="Path to the bb.json file")
    parser.add_argument("--validate", action="store_true",
                        help="Validate shape (schema) and invariants")
    parser.add_argument("--check", action="store_true",
                        help="Check contract invariants only")
    parser.add_argument("--summary", action="store_true", help="Print summary statistics")
    parser.add_argument("--schema", default=None, help="Path to the JSON schema file")
    parser.add_argument("--quiet-warnings", action="store_true",
                        help="Do not print invariant warnings")
    args = parser.parse_args()

    if not os.path.exists(args.json_file):
        print(f"Error: file not found: {args.json_file}", file=sys.stderr)
        sys.exit(1)

    try:
        doc = load_document(args.json_file)
        schema = load_schema(args.schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(2)

    if args.validate:
        errors = validate_document(doc, schema)
        if errors:
            for err in errors[:20]:
                print(f"[ERROR] {err}", file=sys.stderr)
            print(f"[ERROR] Validation FAILED: {len(errors)} error(es) de esquema",
                  file=sys.stderr)
            sys.exit(2)

    if args.validate or args.check:
        errors, warnings = check_invariants(doc)
        if not args.quiet_warnings:
            for warn in warnings:
                print(f"[WARN] {warn}", file=sys.stderr)
        if errors:
            for err in errors[:20]:
                print(f"[ERROR] {err}", file=sys.stderr)
            print(f"[ERROR] Validation FAILED: {len(errors)} invariante(s) rota(s)",
                  file=sys.stderr)
            sys.exit(2)

    if not (args.validate or args.check or args.summary):
        args.summary = True

    if args.validate or args.check:
        nnodes = len(doc.get("nodes", []))
        nedges = len(doc.get("edges", []))
        print(f"[OK] Validation SUCCESS: {nnodes} bloques basicos, {nedges} aristas")
        if not args.summary:
            sys.exit(0)

    if args.summary:
        for line in summarize(doc):
            print(line)


if __name__ == "__main__":
    main()

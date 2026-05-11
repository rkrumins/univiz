#!/usr/bin/env python3
"""
Generate a sample Solidatus JSON model of configurable size.

Produces a realistic data lineage graph with the Solidatus structure:
  Layer → Object → Attribute, with Transitions (lineage) between attributes.

The generated model follows a multi-tier data pipeline pattern:
  Source Systems → Staging → Warehouse → Reporting/Analytics

Usage:
  # Small demo (default: 3 layers, 2-4 objects each, 3-6 attrs each)
  python backend/scripts/generate_solidatus_model.py > model.json

  # Medium (~500 nodes)
  python backend/scripts/generate_solidatus_model.py --layers 6 --objects 5 --attrs 8 > model.json

  # Large (~5000 nodes)
  python backend/scripts/generate_solidatus_model.py --layers 10 --objects 10 --attrs 15 > model.json

  # Pipe directly into the importer
  python backend/scripts/generate_solidatus_model.py --layers 5 | \\
    python backend/scripts/import_solidatus.py --graph solidatus_demo

  # Write to file
  python backend/scripts/generate_solidatus_model.py --layers 8 --output sample_model.json
"""
 
import argparse
import json
import random
import string
import sys
from typing import Dict, List, Any, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# ID GENERATORS
# ═══════════════════════════════════════════════════════════════════════════

def _random_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def layer_id() -> str:
    return f"LYR-{_random_id()}"

def object_id() -> str:
    return f"OBJ-{_random_id()}"

def attribute_id() -> str:
    return f"ATR-{_random_id()}"

def group_id() -> str:
    return f"GRP-{_random_id()}"

def transition_id() -> str:
    return f"TRAN-{_random_id()}"


# ═══════════════════════════════════════════════════════════════════════════
# VOCABULARY — realistic names and types for generated entities
# ═══════════════════════════════════════════════════════════════════════════

LAYER_TEMPLATES = [
    # (name_pattern, description, typical role in pipeline)
    ("CRM System", "Customer relationship management source"),
    ("ERP System", "Enterprise resource planning source"),
    ("Web Analytics", "Clickstream and web event data"),
    ("Payment Gateway", "Transaction processing system"),
    ("HR System", "Human resources data source"),
    ("Marketing Platform", "Campaign and audience data"),
    ("IoT Sensors", "Device telemetry and sensor readings"),
    ("Social Media", "Social media engagement data"),
    ("Staging Area", "Raw data landing zone"),
    ("Data Warehouse", "Curated analytical data store"),
    ("Data Lake", "Semi-structured data repository"),
    ("Reporting Layer", "Business intelligence reporting"),
    ("Analytics Layer", "Advanced analytics and ML features"),
    ("Master Data", "Golden record reference data"),
    ("Compliance Vault", "Regulatory and audit data"),
    ("Customer 360", "Unified customer view"),
    ("Finance Ledger", "General ledger and accounting"),
    ("Supply Chain", "Logistics and inventory data"),
    ("Product Catalog", "Product master and pricing"),
    ("Risk Engine", "Risk scoring and fraud detection"),
]

OBJECT_TEMPLATES = [
    # (name_pattern, owner)
    ("Customers", "Data Engineering"),
    ("Orders", "Data Engineering"),
    ("Transactions", "Finance Team"),
    ("Products", "Product Team"),
    ("Users", "Platform Team"),
    ("Events", "Analytics Team"),
    ("Accounts", "Finance Team"),
    ("Employees", "HR Team"),
    ("Campaigns", "Marketing Team"),
    ("Invoices", "Finance Team"),
    ("Shipments", "Logistics Team"),
    ("Inventory", "Supply Chain"),
    ("Sessions", "Analytics Team"),
    ("Payments", "Finance Team"),
    ("Subscriptions", "Product Team"),
    ("Reviews", "Product Team"),
    ("Tickets", "Support Team"),
    ("Contracts", "Legal Team"),
    ("Budgets", "Finance Team"),
    ("Metrics", "Analytics Team"),
    ("Leads", "Sales Team"),
    ("Partners", "Business Dev"),
    ("Assets", "IT Operations"),
    ("Policies", "Compliance Team"),
    ("Claims", "Insurance Team"),
    ("Positions", "Trading Team"),
    ("Instruments", "Trading Team"),
    ("Counterparties", "Risk Team"),
    ("Exposures", "Risk Team"),
    ("Settlements", "Operations Team"),
]

ATTRIBUTE_TEMPLATES = [
    # (name, data_type)
    ("id", "integer"),
    ("name", "string"),
    ("email", "string"),
    ("phone", "string"),
    ("address", "string"),
    ("city", "string"),
    ("country", "nvarchar"),
    ("postal_code", "string"),
    ("created_at", "datetime"),
    ("updated_at", "datetime"),
    ("status", "string"),
    ("amount", "decimal"),
    ("currency", "string"),
    ("quantity", "integer"),
    ("price", "decimal"),
    ("total", "decimal"),
    ("tax_amount", "decimal"),
    ("discount", "decimal"),
    ("description", "text"),
    ("category", "string"),
    ("type", "string"),
    ("reference_id", "string"),
    ("external_id", "string"),
    ("is_active", "boolean"),
    ("is_deleted", "boolean"),
    ("score", "float"),
    ("rating", "float"),
    ("priority", "integer"),
    ("sequence", "integer"),
    ("start_date", "date"),
    ("end_date", "date"),
    ("due_date", "date"),
    ("owner", "string"),
    ("assignee", "string"),
    ("source", "string"),
    ("region", "string"),
    ("department", "string"),
    ("account_number", "string"),
    ("entity_code", "nvarchar"),
    ("posting_date", "datetime"),
    ("value_date", "date"),
    ("balance", "decimal"),
    ("units", "float"),
    ("weight", "float"),
    ("latitude", "float"),
    ("longitude", "float"),
    ("url", "string"),
    ("hash", "string"),
    ("version", "integer"),
    ("notes", "text"),
]

DATA_CONCEPTS = [
    "Customer Identity", "Financial Amount", "Date / Time", "Geographic Location",
    "Product Reference", "Transaction Reference", "Status Indicator", "Classification",
    "Measurement", "Contact Information", "Legal Entity", "Account",
]

DATA_ELEMENTS = [
    "Primary Key", "Business Name", "Email Address", "Postal Address",
    "Creation Timestamp", "Monetary Value", "Country Of Operations",
    "Record Status", "Quantity Measure", "Reference Code", "Entity Code",
    "Posting Date",
]

DEPARTMENTS = [
    "Data Engineering", "Finance Team", "Analytics Team", "Product Team",
    "Platform Team", "HR Team", "Marketing Team", "Compliance Team",
    "Operations Team", "Risk Team",
]


# ═══════════════════════════════════════════════════════════════════════════
# MODEL GENERATOR
# ═══════════════════════════════════════════════════════════════════════════

class SolidatusModelGenerator:
    """Generate a realistic Solidatus JSON model."""

    def __init__(
        self,
        num_layers: int = 4,
        objects_per_layer: Tuple[int, int] = (2, 4),
        attrs_per_object: Tuple[int, int] = (3, 8),
        groups_chance: float = 0.2,
        transition_density: float = 0.4,
        seed: int = None,
    ):
        self.num_layers = num_layers
        self.objects_per_layer = objects_per_layer
        self.attrs_per_object = attrs_per_object
        self.groups_chance = groups_chance
        self.transition_density = transition_density

        if seed is not None:
            random.seed(seed)

        self.entities: Dict[str, Dict[str, Any]] = {}
        self.layer_ids: List[str] = []
        self.transitions: Dict[str, Dict[str, Any]] = {}

        # Track attributes per layer for lineage generation
        self._layer_attrs: Dict[str, List[str]] = {}  # layer_id → [attr_ids]

    def _pick_layer_name(self, index: int) -> str:
        if index < len(LAYER_TEMPLATES):
            return LAYER_TEMPLATES[index][0]
        return f"Data Layer {index + 1}"

    def _pick_object_name(self, layer_name: str, index: int) -> Tuple[str, str]:
        """Return (object_name, owner)."""
        template = OBJECT_TEMPLATES[index % len(OBJECT_TEMPLATES)]
        # Make object names unique by incorporating the layer context
        short_layer = layer_name.split()[0] if " " in layer_name else layer_name[:6]
        suffix = f" ({short_layer})" if index >= len(OBJECT_TEMPLATES) else ""
        return template[0] + suffix, template[1]

    def _pick_attributes(self, count: int, object_name: str) -> List[Tuple[str, str, Dict]]:
        """Return list of (name, data_type, properties) for attributes."""
        # Always include an ID and a name field, then pick random others
        chosen = list(ATTRIBUTE_TEMPLATES[:2])  # id, name
        remaining = list(ATTRIBUTE_TEMPLATES[2:])
        random.shuffle(remaining)
        chosen.extend(remaining[:count - 2])

        result = []
        for attr_name, data_type in chosen[:count]:
            props: Dict[str, Any] = {
                "DATA_TYPE": data_type,
                "DataOwnerDepartment": random.choice(DEPARTMENTS),
            }
            # Add rich metadata for some attributes
            if random.random() < 0.5:
                props["DataConcept"] = random.choice(DATA_CONCEPTS)
            if random.random() < 0.4:
                props["DataElement"] = random.choice(DATA_ELEMENTS)
                props["DataElementDefinition"] = f"The {attr_name} field of {object_name}."
            result.append((attr_name, data_type, props))
        return result

    def _add_entity(self, eid: str, name: str, properties: Dict, children: List[str] = None):
        entry: Dict[str, Any] = {"name": name, "properties": properties}
        if children:
            entry["children"] = children
        self.entities[eid] = entry

    def generate(self) -> Dict[str, Any]:
        """Generate the full model and return as a dict."""

        # ── Create layers ────────────────────────────────────────────
        layer_names_shuffled = list(range(len(LAYER_TEMPLATES)))
        random.shuffle(layer_names_shuffled)

        for li in range(self.num_layers):
            lid = layer_id()
            self.layer_ids.append(lid)
            lname = self._pick_layer_name(layer_names_shuffled[li % len(LAYER_TEMPLATES)])

            self._layer_attrs[lid] = []
            object_ids: List[str] = []

            # ── Create objects within this layer ─────────────────────
            num_objects = random.randint(*self.objects_per_layer)
            for oi in range(num_objects):
                oid = object_id()
                obj_name, owner = self._pick_object_name(lname, li * 10 + oi)
                attr_ids: List[str] = []

                # ── Maybe create a group within this object ──────────
                if random.random() < self.groups_chance:
                    gid = group_id()
                    grp_attr_ids: List[str] = []
                    num_grp_attrs = random.randint(2, max(2, self.attrs_per_object[0]))
                    grp_attrs = self._pick_attributes(num_grp_attrs, f"{obj_name}/Group")
                    for gattr_name, _, gattr_props in grp_attrs:
                        aid = attribute_id()
                        self._add_entity(aid, gattr_name, gattr_props)
                        grp_attr_ids.append(aid)
                        self._layer_attrs[lid].append(aid)

                    self._add_entity(gid, f"{obj_name} Details", {"GroupType": "metadata"}, grp_attr_ids)
                    attr_ids.append(gid)

                # ── Create attributes ────────────────────────────────
                num_attrs = random.randint(*self.attrs_per_object)
                attrs = self._pick_attributes(num_attrs, obj_name)
                for attr_name, _, attr_props in attrs:
                    aid = attribute_id()
                    self._add_entity(aid, attr_name, attr_props)
                    attr_ids.append(aid)
                    self._layer_attrs[lid].append(aid)

                self._add_entity(oid, obj_name, {"Owner": owner}, attr_ids)
                object_ids.append(oid)

            self._add_entity(lid, lname, {}, object_ids)

        # ── Create transitions (lineage between adjacent layers) ─────
        # Data flows forward: layer[i] attributes → layer[i+1] attributes
        for i in range(len(self.layer_ids) - 1):
            source_layer = self.layer_ids[i]
            target_layer = self.layer_ids[i + 1]

            source_attrs = self._layer_attrs.get(source_layer, [])
            target_attrs = self._layer_attrs.get(target_layer, [])

            if not source_attrs or not target_attrs:
                continue

            # Each target attribute gets lineage from 1-3 source attributes
            for target_aid in target_attrs:
                if random.random() > self.transition_density:
                    continue

                num_sources = random.randint(1, min(3, len(source_attrs)))
                chosen_sources = random.sample(source_attrs, num_sources)

                for source_aid in chosen_sources:
                    tid = transition_id()
                    props: Dict[str, Any] = {}
                    # Add transformation metadata for some transitions
                    if random.random() < 0.3:
                        props["transformationType"] = random.choice([
                            "direct_copy", "concatenation", "aggregation",
                            "lookup", "calculation", "filtering", "pivot",
                        ])
                    if random.random() < 0.2:
                        props["confidence"] = round(random.uniform(0.6, 1.0), 2)

                    self.transitions[tid] = {
                        "source": source_aid,
                        "target": target_aid,
                        "properties": props,
                    }

        return {
            "entities": self.entities,
            "layers": self.layer_ids,
            "transitions": self.transitions,
        }


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a sample Solidatus JSON model of configurable size",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Size examples:
  Tiny    (~ 30 nodes):  --layers 2 --min-objects 1 --max-objects 2 --min-attrs 2 --max-attrs 4
  Small   (~100 nodes):  --layers 4  (defaults)
  Medium  (~500 nodes):  --layers 6 --max-objects 5 --max-attrs 10
  Large  (~5000 nodes):  --layers 10 --max-objects 10 --max-attrs 15
  Huge  (~50000 nodes):  --layers 20 --max-objects 20 --max-attrs 30

Pipeline:
  python backend/scripts/generate_solidatus_model.py --layers 5 | \\
    python backend/scripts/import_solidatus.py --graph solidatus_demo
        """,
    )
    parser.add_argument("--layers", type=int, default=4,
                        help="Number of layers (default: 4)")
    parser.add_argument("--min-objects", type=int, default=2,
                        help="Min objects per layer (default: 2)")
    parser.add_argument("--max-objects", type=int, default=4,
                        help="Max objects per layer (default: 4)")
    parser.add_argument("--min-attrs", type=int, default=3,
                        help="Min attributes per object (default: 3)")
    parser.add_argument("--max-attrs", type=int, default=8,
                        help="Max attributes per object (default: 8)")
    parser.add_argument("--groups-chance", type=float, default=0.2,
                        help="Probability of an object having a group (0.0-1.0, default: 0.2)")
    parser.add_argument("--transition-density", type=float, default=0.4,
                        help="Probability of a target attribute having lineage (0.0-1.0, default: 0.4)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducible output")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output file path (default: stdout)")
    parser.add_argument("--stats", action="store_true",
                        help="Print stats to stderr")

    args = parser.parse_args()

    gen = SolidatusModelGenerator(
        num_layers=args.layers,
        objects_per_layer=(args.min_objects, args.max_objects),
        attrs_per_object=(args.min_attrs, args.max_attrs),
        groups_chance=args.groups_chance,
        transition_density=args.transition_density,
        seed=args.seed,
    )

    model = gen.generate()

    # Stats
    num_entities = len(model["entities"])
    num_layers = len(model["layers"])
    num_transitions = len(model["transitions"])
    if args.stats:
        print(f"Generated: {num_entities} entities, {num_layers} layers, {num_transitions} transitions", file=sys.stderr)

    # Output
    output = json.dumps(model, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
            f.write("\n")
        if not args.stats:
            print(f"Written to {args.output} ({num_entities} entities, {num_transitions} transitions)", file=sys.stderr)
    else:
        print(output)

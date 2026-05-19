from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field
from enum import Enum
from .graph import GraphNode, GraphEdge


class RuleOperator(str, Enum):
    EQUALS = 'equals'
    CONTAINS = 'contains'
    STARTS_WITH = 'startsWith'
    ENDS_WITH = 'endsWith'
    EXISTS = 'exists'
    NOT_EQUALS = 'notEquals'


class RuleCondition(BaseModel):
    field: str
    operator: RuleOperator
    value: Optional[Any] = None


class LayerAssignmentRuleConfig(BaseModel):
    id: str
    priority: int
    entity_types: Optional[List[str]] = Field(None, alias="entityTypes")
    tags: Optional[List[str]] = None
    urn_pattern: Optional[str] = Field(None, alias="urnPattern")
    # Restricts the rule to descendants of this URN via the containment
    # parent chain computed by AssignmentEngine. Lets a rule target
    # "all <T> under entity P" without flattening the subtree into
    # explicit EntityAssignmentConfig rows.
    scope_root_urn: Optional[str] = Field(None, alias="scopeRootUrn")
    conditions: Optional[List[RuleCondition]] = None

    class Config:
        populate_by_name = True


class EntityAssignmentConfig(BaseModel):
    entity_id: str = Field(alias="entityId")
    layer_id: str = Field(alias="layerId")
    logical_node_id: Optional[str] = Field(None, alias="logicalNodeId")
    inherits_children: bool = Field(True, alias="inheritsChildren")
    priority: int
    assigned_by: str = Field(alias="assignedBy")
    assigned_at: str = Field(alias="assignedAt")

    class Config:
        populate_by_name = True


class LogicalNodeConfig(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = Field(None, alias="parentId")
    children: Optional[List['LogicalNodeConfig']] = None
    rules: Optional[List[LayerAssignmentRuleConfig]] = None

    class Config:
        populate_by_name = True


class ViewLayerConfig(BaseModel):
    id: str
    name: str
    color: str
    order: int
    sequence: Optional[int] = None
    entity_types: Optional[List[str]] = Field(None, alias="entityTypes")
    rules: Optional[List[LayerAssignmentRuleConfig]] = None
    logical_nodes: Optional[List[LogicalNodeConfig]] = Field(None, alias="logicalNodes")
    entity_assignments: Optional[List[EntityAssignmentConfig]] = Field(None, alias="entityAssignments")

    class Config:
        populate_by_name = True


class ScopeFilterConfig(BaseModel):
    domain_urns: Optional[List[str]] = Field(None, alias="domainUrns")
    tags: Optional[List[str]] = None

    class Config:
        populate_by_name = True


class LayerAssignmentRequest(BaseModel):
    scope_filter: Optional[ScopeFilterConfig] = Field(None, alias="scopeFilter")
    layers: List[ViewLayerConfig]
    include_edges: bool = Field(True, alias="includeEdges")

    class Config:
        populate_by_name = True


class EntityAssignment(BaseModel):
    entity_id: str = Field(alias="entityId")
    layer_id: str = Field(alias="layerId")
    logical_node_id: Optional[str] = Field(None, alias="logicalNodeId")
    rule_id: Optional[str] = Field(None, alias="ruleId")
    is_inherited: bool = Field(False, alias="isInherited")
    inherited_from_id: Optional[str] = Field(None, alias="inheritedFromId")
    confidence: float = 1.0

    class Config:
        populate_by_name = True


class LayerAssignmentStats(BaseModel):
    total_nodes: int = Field(alias="totalNodes")
    assigned_nodes: int = Field(alias="assignedNodes")
    compute_time_ms: float = Field(alias="computeTimeMs")

    class Config:
        populate_by_name = True


class LayerAssignmentResult(BaseModel):
    assignments: Dict[str, EntityAssignment]
    parent_map: Dict[str, str] = Field(alias="parentMap")
    edges: List[GraphEdge] = Field(default_factory=list)
    unassigned_entity_ids: List[str] = Field(default_factory=list, alias="unassignedEntityIds")
    stats: LayerAssignmentStats

    class Config:
        populate_by_name = True

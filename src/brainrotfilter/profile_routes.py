"""
profile_routes.py - FastAPI router for Family Profile management.

Registers at prefix /api/profiles.  Include this router in the main FastAPI
app in analyzer_service.py:

    from profile_routes import router as profile_router
    app.include_router(profile_router)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from profile_manager import PRESET_PROFILES, Profile, profile_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


# ---------------------------------------------------------------------------
# Pydantic request / response shapes
# ---------------------------------------------------------------------------


class ScheduleRuleIn(BaseModel):
    days: List[int] = Field(..., description="0=Monday…6=Sunday")
    start_time: str = Field(..., description="HH:MM")
    end_time: str = Field(..., description="HH:MM")
    mode: str = Field(..., description="response_mode override")


class ProfileCreateRequest(BaseModel):
    name: str
    ip_ranges: List[str] = Field(default_factory=list)
    mac_addresses: List[str] = Field(default_factory=list)
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    response_mode: str = "standard"
    schedule: List[Dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    is_default: bool = False
    description: str = ""
    # Optionally create from a preset key
    preset_key: Optional[str] = None


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    ip_ranges: Optional[List[str]] = None
    mac_addresses: Optional[List[str]] = None
    thresholds: Optional[Dict[str, Any]] = None
    response_mode: Optional[str] = None
    schedule: Optional[List[Dict[str, Any]]] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    description: Optional[str] = None


class DeviceAssignRequest(BaseModel):
    ip_address: str = ""
    mac_address: str = ""
    device_name: str = ""


class ScheduleSetRequest(BaseModel):
    schedule: List[ScheduleRuleIn]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _profile_to_dict(p: Profile, include_device_count: bool = True) -> Dict[str, Any]:
    d = p.to_dict()
    if include_device_count:
        d["device_count"] = profile_manager.profile_device_count(p.id)  # type: ignore[arg-type]
    return d


def _require_profile(profile_id: int) -> Profile:
    p = profile_manager.get_profile(profile_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found.")
    return p


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", summary="List all profiles")
async def list_profiles() -> Dict[str, Any]:
    """Return all profiles with device counts and current active mode."""
    profiles = profile_manager.list_profiles()
    items = []
    for p in profiles:
        d = _profile_to_dict(p)
        d["active_mode"] = profile_manager.get_active_mode(p)
        items.append(d)
    return {"profiles": items, "total": len(items)}


@router.get("/presets", summary="List built-in preset profiles")
async def list_presets() -> Dict[str, Any]:
    """Return the built-in preset profile definitions."""
    return {
        "presets": [
            {"key": k, **v}
            for k, v in PRESET_PROFILES.items()
        ]
    }


@router.get("/lookup/{ip}", summary="Look up profile for an IP address")
async def lookup_ip(ip: str) -> Dict[str, Any]:
    """
    Resolve which profile applies to the given IP, and return the effective
    thresholds that the decision engine would use.
    """
    profile = profile_manager.get_profile_for_ip(ip)
    thresholds = profile_manager.get_effective_thresholds(ip)
    return {
        "ip": ip,
        "profile": _profile_to_dict(profile) if profile else None,
        "effective_thresholds": thresholds,
    }


@router.post("", summary="Create a new profile", status_code=201)
async def create_profile(body: ProfileCreateRequest) -> Dict[str, Any]:
    """
    Create a profile from scratch or from a built-in preset.

    If *preset_key* is supplied the preset's thresholds/mode are used as base
    values, then overridden by any explicit fields in the request body.
    """
    if body.preset_key:
        # Start from preset, then merge explicit overrides
        if body.preset_key not in PRESET_PROFILES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown preset key: {body.preset_key!r}",
            )
        base = PRESET_PROFILES[body.preset_key]
        merged_thresholds = dict(base.get("thresholds", {}))
        merged_thresholds.update(body.thresholds)
        profile = Profile(
            name=body.name or base["name"],
            ip_ranges=body.ip_ranges,
            mac_addresses=body.mac_addresses,
            thresholds=merged_thresholds,
            response_mode=body.response_mode if body.response_mode != "standard"
            else base.get("response_mode", "standard"),
            schedule=body.schedule,
            enabled=body.enabled,
            is_default=body.is_default,
            preset_key=body.preset_key,
            description=body.description or base.get("description", ""),
        )
    else:
        profile = Profile(
            name=body.name,
            ip_ranges=body.ip_ranges,
            mac_addresses=body.mac_addresses,
            thresholds=body.thresholds,
            response_mode=body.response_mode,
            schedule=body.schedule,
            enabled=body.enabled,
            is_default=body.is_default,
            description=body.description,
        )

    try:
        new_id = profile_manager.create_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"id": new_id, "profile": _profile_to_dict(profile)}


@router.get("/{profile_id}", summary="Get a single profile")
async def get_profile(profile_id: int) -> Dict[str, Any]:
    p = _require_profile(profile_id)
    d = _profile_to_dict(p)
    d["active_mode"] = profile_manager.get_active_mode(p)
    d["devices"] = profile_manager.get_device_assignments(profile_id)
    return d


@router.put("/{profile_id}", summary="Update a profile")
async def update_profile(profile_id: int, body: ProfileUpdateRequest) -> Dict[str, Any]:
    _require_profile(profile_id)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No update fields provided.")
    try:
        changed = profile_manager.update_profile(profile_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not changed:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found.")

    updated = profile_manager.get_profile(profile_id)
    return {"updated": True, "profile": _profile_to_dict(updated)}  # type: ignore[arg-type]


@router.delete("/{profile_id}", summary="Delete a profile")
async def delete_profile(profile_id: int) -> Dict[str, Any]:
    deleted = profile_manager.delete_profile(profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found.")
    return {"deleted": True, "id": profile_id}


@router.post("/{profile_id}/duplicate", summary="Duplicate a profile", status_code=201)
async def duplicate_profile(
    profile_id: int,
    new_name: str = Query(default="", description="Name for the copy"),
) -> Dict[str, Any]:
    _require_profile(profile_id)
    try:
        new_id = profile_manager.duplicate_profile(profile_id, new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    new_profile = profile_manager.get_profile(new_id)
    return {"id": new_id, "profile": _profile_to_dict(new_profile)}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Device assignment sub-routes
# ---------------------------------------------------------------------------


@router.get("/{profile_id}/devices", summary="List devices assigned to a profile")
async def list_devices(profile_id: int) -> Dict[str, Any]:
    _require_profile(profile_id)
    devices = profile_manager.get_device_assignments(profile_id)
    return {"profile_id": profile_id, "devices": devices, "total": len(devices)}


@router.post("/{profile_id}/devices", summary="Assign a device to a profile", status_code=201)
async def assign_device(profile_id: int, body: DeviceAssignRequest) -> Dict[str, Any]:
    _require_profile(profile_id)
    if not body.ip_address and not body.mac_address:
        raise HTTPException(status_code=400, detail="ip_address or mac_address required.")
    try:
        profile_manager.assign_device(
            profile_id,
            ip=body.ip_address,
            mac=body.mac_address,
            name=body.device_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "assigned": True,
        "profile_id": profile_id,
        "device": {
            "ip_address": body.ip_address,
            "mac_address": body.mac_address,
            "device_name": body.device_name,
        },
    }


@router.delete("/{profile_id}/devices/{device_id}", summary="Remove a device assignment")
async def remove_device(profile_id: int, device_id: int) -> Dict[str, Any]:
    _require_profile(profile_id)
    removed = profile_manager.remove_device(device_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Device assignment {device_id} not found.")
    return {"removed": True, "device_id": device_id}


@router.get("/devices/all", summary="List all device assignments across profiles")
async def all_devices() -> Dict[str, Any]:
    """Return every device assignment annotated with profile name."""
    devices = profile_manager.all_device_assignments()
    return {"devices": devices, "total": len(devices)}


@router.get("/devices/scan", summary="Show recently-seen IPs from request logs")
async def scan_network(limit: int = Query(default=50, ge=1, le=200)) -> Dict[str, Any]:
    """
    Return IPs seen recently in the Squid request log, each annotated with
    its current profile assignment.  Use this to quickly discover unassigned devices.
    """
    ips = profile_manager.get_recently_seen_ips(limit=limit)
    return {"ips": ips, "total": len(ips)}


# ---------------------------------------------------------------------------
# Schedule sub-routes
# ---------------------------------------------------------------------------


@router.post("/{profile_id}/schedule", summary="Replace the schedule for a profile")
async def set_schedule(profile_id: int, body: ScheduleSetRequest) -> Dict[str, Any]:
    """Replace the entire schedule rule list for *profile_id*."""
    _require_profile(profile_id)
    rules = [r.model_dump() for r in body.schedule]
    updated = profile_manager.set_schedule(profile_id, rules)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update schedule.")
    p = profile_manager.get_profile(profile_id)
    return {
        "updated": True,
        "profile_id": profile_id,
        "schedule": p.schedule if p else [],  # type: ignore[union-attr]
        "active_mode": profile_manager.get_active_mode(p) if p else "standard",
    }


@router.get("/{profile_id}/schedule/active", summary="Get the currently active mode for a profile")
async def active_mode(profile_id: int) -> Dict[str, Any]:
    p = _require_profile(profile_id)
    mode = profile_manager.get_active_mode(p)
    return {
        "profile_id": profile_id,
        "profile_name": p.name,
        "base_mode": p.response_mode,
        "active_mode": mode,
        "schedule_overriding": mode != p.response_mode,
    }


@router.post("/{profile_id}/set-default", summary="Set profile as the default fallback")
async def set_default(profile_id: int) -> Dict[str, Any]:
    _require_profile(profile_id)
    changed = profile_manager.set_default_profile(profile_id)
    return {"updated": changed, "default_profile_id": profile_id}

"""
Pydantic request/response models for household endpoints.
"""
from pydantic import BaseModel


class HouseholdSetupRequest(BaseModel):
    household_name: str
    display_name: str


class HouseholdSetupResponse(BaseModel):
    household_id: str
    member_id: str
    role: str


class HouseholdMember(BaseModel):
    member_id: str
    display_name: str
    role: str


class InviteResponse(BaseModel):
    code: str
    invite_url: str
    expires_at: str


class JoinRequest(BaseModel):
    code: str
    display_name: str


class JoinResponse(BaseModel):
    household_id: str
    member_id: str
    role: str


class HouseholdInfo(BaseModel):
    household_id: str
    household_name: str

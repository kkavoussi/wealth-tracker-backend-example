"""
Pydantic response models for auth endpoints.
"""
from pydantic import BaseModel


class UserResponse(BaseModel):
    user_id: str
    email: str


class MessageResponse(BaseModel):
    message: str

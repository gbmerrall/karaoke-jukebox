"""
models.py

Defines SQLAlchemy ORM models for the karaoke app.
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
import datetime

Base = declarative_base()

class User(Base):
    """User model for karaoke app."""
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    queue_items = relationship('Queue', back_populates='user')

class Video(Base):
    """Video model for YouTube videos."""
    __tablename__ = 'videos'
    id = Column(Integer, primary_key=True, index=True)
    youtube_id = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=False)
    url = Column(String, nullable=False)
    downloaded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    queue_items = relationship('Queue', back_populates='video')

class Queue(Base):
    """Queue model for song queueing."""
    __tablename__ = 'queue'
    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    added_at = Column(DateTime, default=datetime.datetime.utcnow)
    video = relationship('Video', back_populates='queue_items')
    user = relationship('User', back_populates='queue_items')

class AdminConfig(Base):
    """Admin configuration for storing admin credentials."""
    __tablename__ = 'admin_config'
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)

# Example placeholder model
# class User(Base):
#     __tablename__ = 'users'
#     id = Column(Integer, primary_key=True, index=True)
#     name = Column(String, index=True) 
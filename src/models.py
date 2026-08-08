from typing import List, Optional


class Playlist:
    """Represents a playlist/album with metadata and songs."""
    name: str
    playlist_url: str
    length: int
    songs: List['Song']

    def __init__(self, playlist_url: str, name: str = "", length: int = 0,
                 songs: Optional[List['Song']] = None):
        self.name = name
        self.playlist_url = playlist_url
        self.length = length
        self.songs = songs or []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "playlist_url": self.playlist_url,
            "length": self.length,
            "songs": [song.to_dict() for song in self.songs]
        }

    def __repr__(self) -> str:
        return f"Playlist(name={self.name!r}, length={self.length}, songs={len(self.songs)})"


class Song:
    """Represents a single track with its playlist context."""
    title: str
    artists: List[str]
    song_url: str
    playlist_url: str
    error: str
    playlist: Playlist
    list_position: str

    def __init__(self, song_url: str, playlist_url: str = "", error: str = "",
                 title: str = "", artists: Optional[List[str]] = None,
                 playlist: Optional[Playlist] = None, list_position: str = ""):
        self.title = title
        self.artists = artists or []
        self.song_url = song_url
        self.playlist_url = playlist_url
        self.error = error
        if playlist is None:
            self.playlist = Playlist(playlist_url)
        else:
            self.playlist = playlist
        self.list_position = list_position

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artists": self.artists,
            "song_url": self.song_url,
            "playlist_url": self.playlist_url,
            "error": self.error,
            "playlist": self.playlist.to_dict(),
            "list_position": self.list_position
        }

    def __repr__(self) -> str:
        return f"Song(title={self.title!r}, artists={self.artists}, error={self.error!r})"
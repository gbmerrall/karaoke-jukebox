You are designing an app that allows multiple users to search and queue videos for playback.

You should use the following tech stack
1. You are running inside a pipenv virutal environment. If you need to install packages use 'pipenv install <package-name>'
Make sure you check the Pipfile in case packages have already been installed.
2. FastAPI with Pydantic and SQLAlchemy including defining models
3. Alembic for DB migrations
4. Bootstrap5 for front end UI elements. This app will be mostly viewed on mobile so responsive design is the priority
5. SQLite for the database
6. pynng for event notification
7. Server Side Events (SSE) for managing queue changes
8. pytubefix for YouTube search/download
9. apscheduler for scheduled tasks
10. pychromecast for playback
11. use asyncio 
12. gunicorn for serving
13. Docker for delivery and running

You MAY use HTMX for additional interactivity

## App Flow
1. User is presented with a page asking their name  
   * If the admin username  is entered there should also be a password check. The admin username and password should be stored in a config file
2. After entering their name, they're presented with the main screen which is made up of two main components  
    * Along the top is a scrollable element for the current video queue. This will be covered in detail below
    * The rest of the page is a search UI. When the user firsts enters the page there is a search box with associated buttom

3. The users enters a search string  which is either a song title or artist. Upon form submission backend receives the response and performs a youtube search using pytubefix. The results including the video embed, name and title should appear in the results page ordered by most views. The user should be able to preview the videos in the search results. 
4. There should be a button to "queue" the video. If the user queues the video, the following actions are performed.  
   * Confirm to the user that the video is downloading and will be added to queue shortly using a small modal with an "OK" button to close.
   * Check if the video has already been downloaded. If yes, proceed to skip download and queue the video. 
   * If not downloaded Use pytubefix to download the YouTube video. The video should be saved in a data directory using the unique YouTube ID. Use max video and audio quality available
   * Once downloaded it should be added to the queue

5. The queue is a SQLite table that stores the video ID, username and a timestamp of when it was added to the queue
6. Once added to the queue, send a notification using pynng containing the username and YouTube video ID

## Queue UI feature
1. The queue is a sideways-scrollable horizontal feature in the top of the page shows all songs queued by all users
2. When a user queues a song, a server side event should be triggered that sends an updated queue to the front-end.
3. The queue should include song name and title and the user who queued the wong
4. For any songs that are queued by the user using the app, they should have an option to delete their song from the queue. If deleted, a new queue should be pushed to all users.

## Other features
1. Use the FastAPI decorator @app.on_event("startup") to create a method using apscheduler that runs every 4 hours. It should delete any songs in the queue that have been added greater than 4 hours previously. 

## Admin usage
1. If the admin user is logged in they will have several additional features available to them  
    * The ability to start and stop playout. More on this below
    * The ability to interrupt the currently playing song and advance to the next song in the queue
    * The ability to delete any song from the queue

# Playout
1. Upon the admin user starting playout, the backend scans for available chromecast devices using pychromecast. The admin selects the right device and playout commences
2. The queue is driven by a first in, first out. That is the queue should be played in the order they were added to the database
3. Once playout finishes, or the song is skipped by the admin, or the song is deleted from the queue by the owner or admin, the song should be removed from the database. The queue on the device should also be updated
4. Once one song finishes, the next song in the queue commences playout


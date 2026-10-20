1. Take a CSV as input, example provided at example.CSV.
2. For each song in the list, go find it on youtube music and download the highest quality MP3 possible for it.
3. Store in a configurable 'music' directory in such a way that during step2 we can verify before trying to fetch a song that it already exists. Only match by Artist - Title, don't worry about finding live versions or anything like that.
4. Store a new playlist called local_$inputfilename in a format that MPD, the music player daemon can read

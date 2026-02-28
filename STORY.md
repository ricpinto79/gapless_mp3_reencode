Hi there! Hope you're doing awesome.

I wanted to share the little story behind these two tools, because every good project needs a proper origin tale.
A while back I said “screw it” to all streaming services (both video and music). I’m a huge metalhead with an 800+ CD collection, and I decided it was time to host my own media properly. But here’s the thing: I’m not just a collector, I’m a total sound-quality snob. The random rips you find online? Nope. Not even close to what my ears demand.
So the big question became: how the hell am I going to convert 800+ albums without losing my mind? I started with lossless files and went hunting for a tool that would let me make perfect MP3s with a VBR between 192–320 kbps. 

Fast-forward a few days and a couple dozen albums… and I’m listening to Iron Maiden’s A Real Live One when BAM! gaps between tracks! GAPS! In a live album! Really?!?!
That was the moment I lost it. I stopped, did some research, and discovered the dark truth: normal tools encode track-by-track and completely ignore the magic LAME flags you need for true gapless playback. Oh, and there’s this mysterious -V flag (0–9) that actually controls quality. (V0 is basically audiophile heaven, V2 is the perfect “I’m not crazy but I also don’t want 500 GB of MP3s” sweet spot.) And don’t even get me started on joint stereo vs true stereo!? Of course I want true stereo!

I was already tired of babysitting one album at a time and then manually feeding everything into the MusicBrainz app for tags and covers. That’s when the lazy engineer in me snapped. I’m a mechanical engineer by trade and I deeply love computers. So I decided to build my own tool.
First came the Bulk Gapless MP3 Re-encode tool. Goal: throw a folder full of lossless files/albums/folders at it and get perfectly gapless, perfectly named MP3 albums out the other end... Exactly how I like them.

Halfway through testing I realized I still had to tag and embed covers manually for dozens of albums. So, I built the second tool: MusicBrainz Tagger + Cover Art Embedder. It sniffs your MP3 folders, searches MusicBrainz (prioritizing catalog numbers), lets you pick a compatible release (US, EU, Japanese, whatever), then asks “same cover or do you want a cleaner one?”. Japanese covers love hiding 1/3 of the artwork behind giant text and I hate that, therefore I want the liberty of picking a different cover from the native release.

The workflow is very smooth now:

First run → creates its own little venv, so nothing pollutes your system, in your work/albums folder.
Dry-run mode → checks everything and tells you what’s good and what’s trash, i.e. poorly or encoded files with errors.
Answer a few quick questions (defaults are my personal preferences).
It converts only the OK albums.
Asks if you want to delete the temp files.

Then you just run the second tool on your new ./MP3 folder and it handles the tagging + cover art with full interactive control (and a smart fallback if a release has no cover).
It’s not perfect yet — I still want multicore support (my workstation is bored) and a proper GUI. Nevertheless, it is rock-solid and I’m genuinely happy with it.
I originally built these for myself. Then I showed them to a couple of friends and they basically yelled “put this on GitHub right now!” So, here we are. I didn’t do this for money, there’s a Buy-Me-a-Coffee button only because Grok suggested it... Blame the AI. I just wanted excellent results with almost zero effort, and now I want everyone else to have the same.

So please enjoy, share with your fellow audio nerds, and crank the volume. Life’s too short for gappy MP3s.
Cheers!
Ric

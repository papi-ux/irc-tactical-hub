a desktop companion utility for users waiting in IRC interview queues. it acts as a bridge between your IRC client and your operating system to provide automated speedtests, real-time queue tracking, analytics, and remote push notifications.

<img width="668" height="551" alt="Screenshot 2026-01-22 224117" src="https://github.com/user-attachments/assets/dac4b929-8b6a-464a-8166-9b2615665f2a" />
<img width="668" height="551" alt="Screenshot 2026-01-22 224125" src="https://github.com/user-attachments/assets/674416d0-4ffd-4ff0-a7d8-c4e71ad9effc" />


**Features**

* queue monitoring: displays your current queue position in real-time.  
* wait time estimates: calculates queue "Velocity" (interviews per hour) and provides an ETA.  
* automated speedtests: runs the Ookla Speedtest CLI in the background and generates a valid \!queue link (supporting both legacy numeric IDs and modern UUIDs) without manual copy-pasting.  
* push notifications: Integrates with **ntfy.sh** to send alerts to your phone when:  
  * you reach the Top 5 positions.  
  * the queue starts moving (someone gets interviewed).  
  * you're mentioned in the IRC channel.  
* analytics: automatically logs interview events to track busy hours, total interviews per day, and pass/fail rates.  
* connection recovery: detects netsplits (server disconnects) and automates the re-queueing process when the bot returns.

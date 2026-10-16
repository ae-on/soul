import requests
import json

url = "https://soul.by/wp-json/events-manager/v1/events"
response = requests.get(url)

if response.status_code == 200:
    events = response.json()
    print(f"Найдено событий: {len(events)}")
    for event in events[:5]:  # покажем первые 5
        print(f" - {event.get('event_name')} | {event.get('event_start_date')} {event.get('event_start_time')}")
else:
    print(f"Ошибка: {response.status_code}")
    print(response.text)



import azure.functions as func
import logging
import os
import json
import requests
import uuid
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import timedelta, datetime, date
from icalendar import Calendar, Event


app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="get_cal")
def get_cal(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Read configuration.
    calendars = json.loads(os.getenv("CalendarSources"))
    name = os.getenv("CalendarName")
    days_history = int(os.getenv("CalendarDaysHistory"))

    # Read url parameters
    if req.params.get('show'):
        show = [int(i) for i in req.params.get('show').split(",")]
    else:
        show = None

    if req.params.get('hide'):
        hide = [int(i) for i in req.params.get('hide').split(",")]
    else:
        hide = None

    if show is not None and hide is not None:
            return func.HttpResponse("Invalid show/hide params.",
                status_code=500
            )

    # Set today's date in UTC using built-in zoneinfo (Python 3.9+)
    today = datetime.now(ZoneInfo("UTC")).date()

    # Create the temporary calendar dictionary, we use this to handle duplicate UIDs
    temp_cal = { }

    # Create the combined calendar
    combined_cal = Calendar()
    combined_cal.add("prodid", "-//Combcal//NONSGML//EN")
    combined_cal.add("version", "2.0")
    combined_cal.add("x-wr-calname", name)

    for calendar in calendars:
        # Validate the calendar has an ID
        if calendar.get("Id") is None:
            return func.HttpResponse(f"Invalid calendar source configuration", status_code=500)
        
        if ((show is None and hide is None) or (show is None and calendar.get('Id') not in hide) or (show is not None and calendar.get('Id') in show)):
            try:
                response = requests.get(calendar["Url"])
                response.raise_for_status()
            except requests.RequestException as err:
                return func.HttpResponse(f"Unable to fetch calendar with id {calendar.get('Id')}", status_code=500)

            try:
                ical = Calendar.from_ical(response.text)
            except Exception as err:
                return func.HttpResponse(f"Unable to parse calendar with id {calendar.get('Id')}", status_code=500)

            for component in ical.walk("VEVENT"):
                end = component.get("dtend")

                # Only show configured historical events
                if end and days_history: 
                    dt_val = end.dt
                    if isinstance(dt_val, datetime):
                        event_date = dt_val.date()
                    else:
                        event_date = dt_val
                    # Filter out older non-recurring
                    if event_date < today - timedelta(days=days_history) and "RRULE" not in component:
                        continue

                copied_event = Event()
                # Copy all properties from the original event
                for key, value in component.items():
                    if isinstance(value, list):
                        for item in value:
                            copied_event.add(key, item)
                    else:
                        copied_event.add(key, value)


                # Set duration if specified
                if calendar.get("Duration") is not None and isinstance(copied_event.DTSTART, datetime):
                    copied_event.DURATION = timedelta(minutes=calendar.get("Duration"))
              
                # If there is no duration or end time set appropriately. If we have the wrong data type ignore the record. 
                elif copied_event.DTSTART is None and copied_event.DURATION is None:
                    if isinstance(copied_event.DTSTART, datetime):
                        copied_event.DURATION = timedelta(minutes=5)
                    elif isinstance(copied_event.DTSTART, date):
                        copied_event.DURATION = timedelta(days=1)
                    else:
                        continue


                # Add padding
                if calendar.get("PadStartMinutes") is not None and isinstance(copied_event.DTSTART, datetime):
                    copied_event.DTSTART = copied_event.DTSTART - timedelta(hours=0, minutes=calendar.get("PadStartMinutes"))
                    copied_event.DURATION = copied_event.duration + timedelta(hours=0, minutes=calendar.get("PadStartMinutes"))

                
                # Add prefix
                if calendar.get("Prefix") is not None:
                    copied_event['SUMMARY'] = f"{calendar.get('Prefix')}: {copied_event['SUMMARY']}"

                # Update UID to a GUID format.
                if calendar.get("MakeUnique") is not None and calendar.get("MakeUnique"):              
                    # If two calendars may have the same event, and we don't want the second calendar in the configuration
                    # to overwrite the first calendar, we can force a unique UID. 
                    copied_event['UID'] = create_uid(f"{calendar.get('Id')}-{copied_event['UID']}")
                else:
                    copied_event['UID'] = create_uid(copied_event['UID'])

                # Remove Organizer
                copied_event.pop("ORGANIZER", None)
                
                # Remove empty lines from the description, since they aren't technically supported by the ICalendar RFC
                if copied_event.get("DESCRIPTION"):
                    copied_event["DESCRIPTION"] = os.linesep.join([s for s in copied_event.get("DESCRIPTION").splitlines() if s])

                temp_cal[copied_event['UID']] = copied_event
                
    
    # Add temp_cal items to combined_cal
    for e in temp_cal.values():
        combined_cal.add_component(e)

    # Add missing timezones
    combined_cal.add_missing_timezones()

    # Write the combined calendar to the output file in binary mode.
    return func.HttpResponse(combined_cal.to_ical(),
                status_code=200
            )

def create_uid(input_string):
    string_bytes = input_string.encode('utf-8')
    hashed_bytes = hashlib.sha1(string_bytes).digest()
    guid = uuid.uuid5(uuid.NAMESPACE_DNS, hashed_bytes.hex())
    return str(guid)

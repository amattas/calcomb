import azure.functions as func
import logging
import os
import re
import json
import requests
import uuid
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import timedelta, datetime, date
from icalendar import Calendar, Event
from dateutil.rrule import rrulestr


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

    # Create the combined calendar
    combined_cal = Calendar()
    combined_cal.add("prodid", "-//Combcal//NONSGML//EN")
    combined_cal.add("version", "2.0")
    combined_cal.add("x-wr-calname", name)

    # Create the temporary calendar dictionary, we use this to handle duplicate UIDs
    temp_cal = { }

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
                elif copied_event.DTEND is None and copied_event.DURATION is None:
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

                # Update UID to a unique value if specified
                if calendar.get("MakeUnique") is not None and calendar.get("MakeUnique"):              
                    # If two calendars may have the same event, and we don't want the second calendar in the configuration
                    # to overwrite the first calendar, we can force a unique UID. The start datetime is included in the hash
                    # because of how Apple handles changing individual instances of a recurring event.
                    copied_event['UID'] = create_uid(f"{calendar.get('Id')}-{copied_event['UID']}")
                # If we aren't updating it, we still want to make sure the UID is a guid, otherwise it can cause problems
                # with Outlook. But we don't want to overwrite it otherwise, because calendars coming from Apple Calendar
                # uses GUIDs, and if you update it, it will break recurring meetings.
                else:
                    guid_regex = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
                    if not guid_regex.match(copied_event['UID']):
                        copied_event['UID'] = create_uid(f"{copied_event['UID']}")

                # Remove Organizer
                copied_event.pop("ORGANIZER", None)
                
                # Remove empty lines from the description, since they aren't technically supported by the ICalendar RFC
                if copied_event.get("DESCRIPTION"):
                    desc_str = copied_event.decoded("DESCRIPTION").decode("utf-8", errors="ignore")
                    # Now desc_str is a normal Python string
                    desc_str = "\n".join(line for line in desc_str.splitlines() if line.strip())
                    copied_event["DESCRIPTION"] = desc_str.strip()
                
                # Normalize date/time properties
                # Single date/time fields: DTSTART, DTEND, RECURRENCE-ID
                # Preserve timezone-aware datetimes to maintain local times and DST behavior; leave naive datetimes as-is
                for dtprop in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                    original = copied_event.pop(dtprop, None)
                    if original is not None:
                        # Extract python date or datetime and convert to UTC if timezone-aware
                        if isinstance(original, datetime):
                            dt = original.astimezone(ZoneInfo("UTC"))
                        elif hasattr(original, 'dt'):
                            dt_val = original.dt
                            if isinstance(dt_val, datetime) and dt_val.tzinfo is not None:
                                dt = dt_val.astimezone(ZoneInfo("UTC"))
                            else:
                                dt = dt_val
                        else:
                            dt = original
                        # Add back in UTC timezone
                        copied_event.add(dtprop, dt)
                # Multi-value recurrence date fields: RDATE, EXDATE 
                # Preserve timezone-aware datetimes; leave naive datetimes as-is
                for listprop in ("RDATE", "EXDATE"):
                    prop_list = copied_event.pop(listprop, None)
                    if prop_list is not None:
                        # Ensure list
                        vals = prop_list if isinstance(prop_list, list) else [prop_list]
                        merged = []
                        for pv in vals:
                            # vDDDList support
                            dates = getattr(pv, 'dts', None)
                            if dates is not None:
                                for d in dates:
                                    merged.append(d.dt)
                            # plain list
                            elif isinstance(pv, list):
                                merged.extend(pv)
                            # single date/datetime
                            else:
                                merged.append(pv)
                        # Add items without converting timezone
                        new_list = []
                        for dt in merged:
                            new_list.append(dt)
                        copied_event.add(listprop, new_list)
                                                    
                # Some calendars we want to force to de-duplicate, for example. TeamSnap calendars have a calender of games and practices
                # and a calendar of just games. We load them both, but add padding to the games for the arrival time. Since we load the
                # game calendar second, and all the events have the same UIDs we want to make sure we aren't duplicating them in the feed.
                # This is simply handled by adding events to a dictionary first. 
                if calendar.get("FilterDuplicates") is not None and calendar.get("FilterDuplicates"):
                    temp_cal[copied_event['UID']] = copied_event
                # For others like Apple Calendar, duplicate UIDs are expectedd for recurring events, so we want to add them directly to 
                # maintain all of the data.
                else:
                    combined_cal.add_component(copied_event)
                
    
    # Add all the deduplicated events to the calendar.
    for e in temp_cal.values():
        combined_cal.add_component(e)

    # Merge Apple-split recurring events (iCloud DST splits) into single series
    combined_cal = merge_recurring_sets(combined_cal)

    # Add missing timezones
    combined_cal.add_missing_timezones()

    # Write the combined calendar to the output file in binary mode.
    headers = {"Content-Type": "text/calendar"}
    response = func.HttpResponse(body=combined_cal.to_ical(),
                status_code=200, headers=headers
            )
    return response

def create_uid(input_string):
    string_bytes = input_string.encode('utf-8')
    hashed_bytes = hashlib.sha1(string_bytes).digest()
    guid = uuid.uuid5(uuid.NAMESPACE_DNS, hashed_bytes.hex())
    return str(guid)


def merge_recurring_sets(cal: Calendar) -> Calendar:
    """
    Merge VEVENTs that share the same RELATED-TO (iCloud recurrence-set) into single VEVENTs
    with an RDATE listing all occurrences.
    """
    new_cal = Calendar()
    # Copy top-level calendar properties
    for name, value in cal.items():
        new_cal.add(name, value)
    grouped = {}
    others = []
    # Partition components by recurrence-set
    for comp in cal.subcomponents:
        if not isinstance(comp, Event):
            new_cal.add_component(comp)
            continue
        recset = comp.get('RELATED-TO')
        if recset:
            key = str(recset)
            grouped.setdefault(key, []).append(comp)
        else:
            others.append(comp)
    # Add non-grouped events
    for ev in others:
        new_cal.add_component(ev)
    # Merge grouped events
    for key, evs in grouped.items():
        if len(evs) == 1:
            new_cal.add_component(evs[0])
            continue
        base = evs[0]
        all_dtstarts = []
        for ev in evs:
            dt0 = ev.decoded('DTSTART')
            rrule_prop = ev.get('RRULE')
            if rrule_prop:
                rule_str = rrule_prop.to_ical().decode()
                occurrences = list(rrulestr(rule_str, dtstart=dt0))
                all_dtstarts.extend(occurrences)
            else:
                all_dtstarts.append(dt0)
        # Deduplicate and sort
        unique_dates = sorted(set(all_dtstarts))
        # Remove old recurrence properties
        base.pop('RRULE', None)
        base.pop('SEQUENCE', None)
        # Add RDATE with all occurrences
        base.add('RDATE', unique_dates)
        new_cal.add_component(base)
    return new_cal

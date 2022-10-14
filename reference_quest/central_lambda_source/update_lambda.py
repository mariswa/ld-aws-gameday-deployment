# Copyright 2022 Amazon.com and its affiliates; all rights reserved. 
# This file is Amazon Web Services Content and may not be duplicated or distributed without permission.
import json
import os
from platform import release
from tabnanny import check
import boto3
from datetime import datetime
import dynamodb_utils
import quest_const
import input_const
import output_const
import scoring_const
import hint_const
import http.client
from aws_gameday_quests.gdQuestsApi import GameDayQuestsApiClient

# Standard AWS GameDay Quests Environment Variables
QUEST_ID = os.environ['QUEST_ID']
QUEST_API_BASE = os.environ['QUEST_API_BASE']
QUEST_API_TOKEN = os.environ['QUEST_API_TOKEN']
GAMEDAY_REGION = os.environ['GAMEDAY_REGION']

# Quest Environment Variables
QUEST_TEAM_STATUS_TABLE = os.environ['QUEST_TEAM_STATUS_TABLE']

# Dynamo DB resource
dynamodb = boto3.resource('dynamodb')
quest_team_status_table = dynamodb.Table(QUEST_TEAM_STATUS_TABLE)

def check_webapp(apprunnerurl):
    try:
        print(f"Testing web app status using URL {apprunnerurl}")
        conn = http.client.HTTPSConnection(apprunnerurl.strip('https://'), timeout=5)
        conn.request("GET", "/")
        res = conn.getresponse()
        status = res.status
        print(f"The status code returned it {status}")
        if status != 200:
            raise Exception(f"Web app down: {status}")
        return True
    except Exception as e:
        print(f"Web app not available: {e}")
        return False

def getAppRelease(team_data):
    try:
        print(f"Getting current version via API")
        conn = http.client.HTTPSConnection(team_data['app-runner-url'].strip('https://'), timeout=5)
        conn.request("GET", "/status")
        res = conn.getresponse()
        string = res.read().decode('utf-8')
        dataResp = json.loads(string)
        print(dataResp['app-version'])
        if dataResp['app-version'] == team_data['app-version']:
            return True
        else:
            raise Exception(f"The version does not match")
    except Exception as e:
        print(f"The version does not match")
        return False

def getDebugValue(team_data):
    try:
        print(f"Getting debug value via the /teamdebug API")
        conn = http.client.HTTPSConnection(team_data['app-runner-url'].strip('https://'), timeout=5)
        conn.request("GET", "/teamdebug")
        res = conn.getresponse()
        string = res.read().decode('utf-8')
        dataResp = json.loads(string)
        print(dataResp['debugcode'])
        if dataResp['debugcode'] == team_data['debugcode']:
            return True
        else:
            raise Exception(f"The debug code is invalid")
    except Exception as e:
        print(f"The version does not match")
        return False

# This function is triggered by sns_lambda.py whenever the team has provided input via the event UI. It validates
# the input and performs related operations, such as updating the team's DynamoDB table record or posting a feedback message.
# Expected event parameters: {'team_id': team_id,'key': key, 'value': value}
def lambda_handler(event, context):
    print(f"update_lambda invocation, event:{json.dumps(event, default=str)}, context: {str(context)}")

    # Instantiate the Quest API Client.
    quests_api_client = GameDayQuestsApiClient(QUEST_API_BASE, QUEST_API_TOKEN)

    # Check if event is running
    event_status = quests_api_client.get_event_status()
    if event_status['status'] != quest_const.EVENT_IN_PROGRESS:
        print(f"Event Status: {event_status['status']}, aborting UPDATE_LAMBDA")
        return

    # Check if quest is active for the team
    quest_status = quests_api_client.get_quest_for_team(team_id=event['team_id'], quest_id=QUEST_ID)
    if quest_status['quest-state'] != quest_const.TEAM_QUEST_IN_PROGRESS:
        print(f"Quest Status: {quest_status['quest-state']}, aborting UPDATE_LAMBDA")

    dynamodb_response = quest_team_status_table.get_item(Key={'team-id': event['team_id']})
    print(f"Retrieved team state for team {event['team_id']}: {json.dumps(dynamodb_response, default=str)}")
    team_data = dynamodb_response['Item']

    # Task 1 evaluation
    if (event['key'] == input_const.TASK1_ENDPOINT_KEY
        and not team_data['is-webapp-up']): # This second check is needed to avoid multiple submissions since points are being given here
        try:
            print("prior apprunner url value is "+team_data['app-runner-url'])
            input_value = event['value'] 
            print("event value is "+event['value'])
            print("input value matches event value - moving through")
            team_data['monitoring-chaos-timer'] = int(datetime.now().timestamp())
            print("setting task1-attempted to True")
            team_data['task1-attempted'] = True
            print("Setting App Runner URL value")
            team_data['app-runner-url'] = str(event['value'])

            # Update DynamoDB to avoid race conditions, then do the rest on success
            dynamodb_utils.save_team_data(team_data, quest_team_status_table)

            # Delete input since cannot be updated as task can be started only once
            quests_api_client.delete_input(
                team_id=team_data["team-id"],
                quest_id=QUEST_ID, 
                key=input_const.TASK1_ENDPOINT_KEY
            )

            apphealth = check_webapp(team_data['app-runner-url'])

            if apphealth == True:
                team_data['is-webapp-up'] = True
                print(f"Setting team_data is-webapp-up to True and updating Dynamo")
                # team_data['is-apprunner-done'] = True
                # print(f"Updating the apprunner-done task to true")
                dynamodb_utils.save_team_data(team_data, quest_team_status_table)
                
                quests_api_client.post_score_event(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID,
                    description=scoring_const.CORRECT_APPRUNNER_DESC,
                    points=scoring_const.CORRECT_APPRUNNER_POINTS
                )              
                # Delete app down message if present
                quests_api_client.delete_output(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    key=output_const.TASK1_APPRUNNER_DOWN_KEY
                )

                quests_api_client.delete_output(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    key=output_const.TASK1_APPRUNNER_WRONG_KEY
                )

                quests_api_client.delete_hint(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    hint_key=hint_const.TASK1_HINT1_KEY,
                    detail=True
                )

            else: 
                if team_data['task1-attempted']:
                    print(f"The web application for team {team_data['team-id']} is DOWN")
                    print(f"Resetting app-runner-url to try again and updating Dynamo")
                    team_data['app-runner-url'] = 'unknown'
                    # team_data['is-apprunner-done']: False
                    print(f"Setting app-runner-url to unknown Dynamo")
                    dynamodb_utils.save_team_data(team_data, quest_team_status_table)

                    quests_api_client.post_input(
                        team_id=team_data['team-id'],
                        quest_id=QUEST_ID,
                        key=input_const.TASK1_ENDPOINT_KEY,
                        label=input_const.TASK1_ENDPOINT_LABEL,
                        description=input_const.TASK1_ENDPOINT_DESCRIPTION,
                        dashboard_index=input_const.TASK1_ENDPOINT_INDEX
                    )
                
                    quests_api_client.post_output(
                        team_id=team_data['team-id'],
                        quest_id=QUEST_ID,
                        key=output_const.TASK1_APPRUNNER_WRONG_KEY,
                        label=output_const.TASK1_APPRUNNER_WRONG_LABEL,
                        value=output_const.TASK1_APPRUNNER_WRONG_VALUE,
                        dashboard_index=output_const.TASK1_APPRUNNER_WRONG_INDEX,
                        markdown=output_const.TASK1_APPRUNNER_WRONG_MARKDOWN,
                    )

                    # Post task 1 hint
                    quests_api_client.post_hint(
                        team_id=team_data['team-id'],
                        quest_id=QUEST_ID,
                        hint_key=hint_const.TASK1_HINT1_KEY,
                        label=hint_const.TASK1_HINT1_LABEL,
                        description=hint_const.TASK1_HINT1_DESCRIPTION,
                        value=hint_const.TASK1_HINT1_VALUE,
                        dashboard_index=hint_const.TASK1_HINT1_INDEX,
                        cost=hint_const.TASK1_HINT1_COST,
                        status=hint_const.STATUS_OFFERED
                    )

                    quests_api_client.post_score_event(
                        team_id=team_data["team-id"],
                        quest_id=QUEST_ID,
                        description=scoring_const.WRONG_APPRUNNER_DESC,
                        points=scoring_const.WRONG_APPRUNNER_POINTS
                    ) 

                    quests_api_client.delete_output(
                        team_id=team_data["team-id"],
                        quest_id=QUEST_ID,
                        key="TASK1_APPRUNNER_DOWN_KEY"
                    ) 

                # Assuming they havent yet 
                else: 
                    print("Not attempted yet; doing nothing")

        except Exception as err:
            print(f"Error while handling team update request: {err}")

    # Task 2 Website Release 
    if (event['key'] == input_const.TASK2_LAUNCH_KEY and not team_data['is-website-released']):
        print("Removing input and locking the scoring")
        quests_api_client.delete_input(
            team_id=team_data["team-id"],
            quest_id=QUEST_ID, 
            key=input_const.TASK2_LAUNCH_KEY
        )
        task2_Score_Lock = team_data['task2-score-locked']
        if not task2_Score_Lock:
            team_data['task2-score-locked'] = True
            input_value = event['value']
            print("The input value is "+input_value)
            team_data['task2-attempted'] = True
            team_data['app-version'] = input_value
            released_version = getAppRelease(team_data)
            print("The value is the app status is "+str(released_version))
            dynamodb_utils.save_team_data(team_data, quest_team_status_table)
            if released_version:
                team_data['is-website-released'] = True
                team_data['start-task-3'] = True
                print("writing updated task2 value")
                dynamodb_utils.save_team_data(team_data, quest_team_status_table)

                quests_api_client.delete_input(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    key=input_const.TASK2_LAUNCH_KEY
                )
                
                print("Task 2 - Checking for errors and deleting if there is")
                
                # Delete Score Lock Ouput here eventually key="task2_score_lock"
                
                quests_api_client.delete_output(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key=output_const.TASK2_UNRELEASED_KEY,
                )
                
                quests_api_client.delete_hint(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    hint_key=hint_const.TASK2_HINT1_KEY,
                    detail=True
                )

                quests_api_client.post_output(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key=output_const.TASK2_COMPLETE_KEY,
                    label=output_const.TASK2_COMPLETE_LABEL,
                    value=output_const.TASK2_COMPLETE_VALUE,
                    dashboard_index=output_const.TASK2_COMPLETE_INDEX,
                    markdown=output_const.TASK2_COMPLETE_MARKDOWN,
                )

                quests_api_client.post_score_event(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID,
                    description=scoring_const.COMPLETE_DESC,
                    points=scoring_const.COMPLETE_POINTS
                )
            else: 
                print("resetting app-version")
                team_data['app-version'] = 'unknown'
                print("writing values to Dynamo")
                dynamodb_utils.save_team_data(team_data, quest_team_status_table)
                
                quests_api_client.post_output(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key="task2_app_unreleased",
                    label="Still unreleased!",
                    value="The preview version of the page is still running",
                    dashboard_index=21,
                    markdown=True,
                )
                
                # TASK2_UNRELEASED_KEY="task2_app_unreleased"
                # TASK2_UNRELEASED_LABEL="Still unreleased!"
                # TASK2_UNRELEASED_INDEX=21
                # TASK2_UNRELEASED_VALUE="The preview version of the page is still running"
                # TASK2_UNRELEASED_MARKDOWN=True

                quests_api_client.post_score_event(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID,
                    description=scoring_const.UNRELEASED_DESC,
                    points=scoring_const.UNRELEASED_POINTS
                )
                team_data['task2-score-locked'] = False
                
                print("Deleting task lock message")
                quests_api_client.delete_output(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key="task2_score_lock",
                )

                quests_api_client.post_input(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key=input_const.TASK2_LAUNCH_KEY,
                    label=input_const.TASK2_LAUNCH_LABEL,
                    description=input_const.TASK2_LAUNCH_DESCRIPTION,
                    dashboard_index=input_const.TASK2_LAUNCH_INDEX
                )
                dynamodb_utils.save_team_data(team_data, quest_team_status_table)
        else:
            quests_api_client.post_output(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key="task2_score_lock",
                    label="Input Locked",
                    value="It looks like a value has already been submitted - scoring is locked. Patience is a virtue",
                    dashboard_index=14,
                    markdown=True,
                )

    # Task 3 Debug Mode Enablement
    elif event['key'] == input_const.TASK3_DEBUG_KEY and not team_data['is-debug-mode'] and not team_data['task3-score-locked']: # run if debug mode is false and task lock is off
        try:
            print("Removing input and locking the scoring")
            quests_api_client.delete_input(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    key=input_const.TASK3_DEBUG_KEY
                )
            team_data['task3-score-locked'] = True
            dynamodb_utils.save_team_data(team_data, quest_team_status_table)
            team_data['task3-attempted'] = True
            input_value = event['value']
            team_data['debugcode'] = input_value
            debugstatus = getDebugValue(team_data)
            if debugstatus:
                quests_api_client.post_output(
                        team_id=team_data['team-id'],
                        quest_id=QUEST_ID,
                        key=output_const.TASK3_COMPLETE_KEY,
                        label=output_const.TASK3_COMPLETE_LABEL,
                        value=output_const.TASK3_COMPLETE_VALUE,
                        dashboard_index=output_const.TASK3_COMPLETE_INDEX,
                        markdown=output_const.TASK3_COMPLETE_MARKDOWN,
                    )

                quests_api_client.post_score_event(
                        team_id=team_data["team-id"],
                        quest_id=QUEST_ID,
                        description=scoring_const.DEBUG_RIGHT_DESC,
                        points=scoring_const.DEBUG_RIGHT_POINTS
                    )
                team_data['is-debug-mode'] = True
                dynamodb_utils.save_team_data(team_data, quest_team_status_table)
            else:
                print("Value was wrong - setting reset sequence") 
                print("Removing score lock for Task 3")
                team_data['task3-score-locked'] = False
                team_data['debugCode'] = 'unknown'
                quests_api_client.post_input(
                        team_id=team_data['team-id'],
                        quest_id=QUEST_ID,
                        key=input_const.TASK3_DEBUG_KEY,
                        label=input_const.TASK3_DEBUG_LABEL,
                        description=input_const.TASK3_DEBUG_DESCRIPTION,
                        dashboard_index=input_const.TASK3_DEBUG_INDEX
                    )  

                quests_api_client.post_score_event(
                        team_id=team_data["team-id"],
                        quest_id=QUEST_ID,
                        description=scoring_const.DEBUG_WRONG_DESC,
                        points=scoring_const.DEBUG_WRONG_POINTS
                    )

                dynamodb_utils.save_team_data(team_data, quest_team_status_table)

        except Exception as err:
            print(f"Error while handling team update request: {err}")


    # Task 4 evaluation
    elif event['key'] == input_const.TASK4_KEY:

        # Check team's input value
        value = event['value'].strip().lower()
        if value in input_const.TASK4_KEY_CORRECT_ANSWERS:

            # Correct answer - switch flag to true
            team_data['is-answer-to-life-correct'] = True

            try:
                # First update DynamoDB to avoid race conditions, then do the rest on success
                dynamodb_utils.save_team_data(team_data, quest_team_status_table)

                # Delete previous error if present
                quests_api_client.delete_output(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    key=output_const.TASK4_WRONG_KEY
                )

                # Delete input since cannot be updated as task can be started only once
                quests_api_client.delete_input(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID, 
                    key=input_const.TASK4_KEY
                )

                # Post output
                quests_api_client.post_output(
                    team_id=team_data['team-id'],
                    quest_id=QUEST_ID,
                    key=output_const.TASK4_CORRECT_KEY,
                    label=output_const.TASK4_CORRECT_LABEL,
                    value=output_const.TASK4_CORRECT_VALUE,
                    dashboard_index=output_const.TASK4_CORRECT_INDEX,
                    markdown=output_const.TASK4_CORRECT_MARKDOWN,
                )

                # Award points
                quests_api_client.post_score_event(
                    team_id=team_data["team-id"],
                    quest_id=QUEST_ID,
                    description=scoring_const.THE_ANSWER_CORRECT_DESC,
                    points=scoring_const.THE_ANSWER_CORRECT_POINTS
                )

            except Exception as err:
                print(f"Error while handling team update request: {err}")

        else:
            print(f"Received wrong answer '{event['value']}' from the team")

            # Post output
            quests_api_client.post_output(
                team_id=team_data['team-id'],
                quest_id=QUEST_ID,
                key=output_const.TASK4_WRONG_KEY,
                label=output_const.TASK4_WRONG_LABEL,
                value=output_const.TASK4_WRONG_VALUE,
                dashboard_index=output_const.TASK4_WRONG_INDEX,
                markdown=output_const.TASK4_WRONG_MARKDOWN,
            )

            # Detract points
            quests_api_client.post_score_event(
                team_id=team_data["team-id"],
                quest_id=QUEST_ID,
                description=scoring_const.THE_ANSWER_WRONG_DESC,
                points=scoring_const.THE_ANSWER_WRONG_POINTS
            )

    else:
        print(f"Unknown input key {event['key']} encountered, ignoring.")

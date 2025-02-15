from fastapi import FastAPI, HTTPException, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Any
import rlcard
import random
import json
from pathlib import Path
import traceback

app = FastAPI()

# Create a directory for static files and templates if they don't exist
Path("static").mkdir(exist_ok=True)
Path("templates").mkdir(exist_ok=True)

# Mount static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Store active games
games = {}


def create_game_env() -> Any:
    env = rlcard.make("uno")
    return env


@app.get("/", response_class=HTMLResponse)
async def get_game(request: Request):
    return templates.TemplateResponse("game.html", {"request": request})


@app.post("/game/create")
async def create_game(num_players: int = 2):
    # if num_players < 2 or num_players > 4:
    #     raise HTTPException(status_code=400, detail="Number of players must be between 2 and 4")

    if num_players != 2:
        raise HTTPException(
            status_code=400, detail="Number of players must be exactly 2"
        )

    game_id = random.randint(1000, 9999)
    env = create_game_env()
    state, _ = env.reset()

    games[game_id] = {
        "env": env,
        "state": state,
        "num_players": num_players,
        "current_player": 0,
        "players": {"human": [], "agent": []},
        "websockets": {},
    }

    return {"game_id": game_id}


@app.post("/game/{game_id}/join")
async def join_game(game_id: int, player_type: str):
    if game_id not in games:
        raise HTTPException(status_code=404, detail="Game not found")

    game = games[game_id]
    total_players = len(game["players"]["human"]) + len(game["players"]["agent"])

    if total_players >= game["num_players"]:
        raise HTTPException(status_code=400, detail="Game is full")

    player_id = total_players
    if player_type == "human":
        game["players"]["human"].append(player_id)
    else:
        game["players"]["agent"].append(player_id)

    return {"player_id": player_id}


@app.websocket("/ws/{game_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: int, player_id: int):
    await websocket.accept()

    print("Player:", player_id, "Game:", game_id)

    if game_id not in games:
        print("Game not found")
        await websocket.close(code=4000, reason="Game not found")
        return

    game = games[game_id]
    game["websockets"][player_id] = websocket

    await broadcast_state(game, player_id)

    try:
        while True:
            data = await websocket.receive_text()
            action_data = json.loads(data)

            print("Action data", action_data)

            if (
                game["current_player"] != player_id
            ):  # temporarily allowing to control both players from the same UI
                print("can't play your move dude")
                await send_error(game, "this isn't your move dude", player_id)
                continue

            state = game["state"]
            env = game["env"]
            action = action_data["action"]

            if action in state["raw_obs"]["legal_actions"]:
                print("User took legal action", action)
                next_state, next_player_id = env.step(action, raw_action=True)
                game["state"] = next_state
                game["current_player"] = next_player_id

                # Play game with AI until user move is required
                while next_player_id != player_id:
                    agent_action = game["state"]["raw_obs"]["legal_actions"][
                        0
                    ]  # always pick first legal action
                    print("AI took the move:", agent_action)
                    next_state, next_player_id = env.step(agent_action, raw_action=True)
                    game["state"] = next_state
                    game["current_player"] = next_player_id

                print("Gonna broadcast; next player is", next_player_id)
                await broadcast_state(game, next_player_id)

                if env.game.is_over():
                    await end_game(game, winner=env.game.round.winner)
            else:
                await send_error(game, f"Illegal move {action} not allowed", player_id)
    except Exception as e:
        print("Deleting game state; err:", e)
        traceback.print_exc()
        del game["websockets"][player_id]
        await send_error(game, "You did something fishy. Ending game", player_id)
        await end_game(game)


async def broadcast_state(game, target_player_id: int):
    state = game["state"]
    for player_id, ws in game["websockets"].items():
        if player_id != target_player_id:
            continue

        print("broadcasting to player", player_id)

        player_state = {
            "hand": state["raw_obs"]["hand"],
            "target": state["raw_obs"]["target"],
            "played_cards": state["raw_obs"]["played_cards"],
            "legal_actions": state["raw_obs"]["legal_actions"],
            "num_cards": state["raw_obs"]["num_cards"],
            "num_players": game["num_players"],
            "current_player": game["current_player"],
            "game_id": id(game),
        }
        await ws.send_json(player_state)


async def end_game(game, winner: int = -1):
    print("ending game", game)
    for ws in game["websockets"].values():
        await ws.send_json({"type": "game_over", "winner": 0})


async def send_error(game, msg: str, target_player_id: int):
    for player_id, ws in game["websockets"].items():
        if player_id == target_player_id:
            await ws.send_json({"type": "error", "message": msg})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001) # localhost:8001/uno

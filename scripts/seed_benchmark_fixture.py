#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import django

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "judge_vortex.settings")
django.setup()

from django.contrib.auth.hashers import make_password  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.authtoken.models import Token  # noqa: E402

from core_api.models import ExamQuestion, ExamRoom, RoomParticipant, UserProfile  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed a benchmark fixture for Judge Vortex.")
    parser.add_argument("--users", type=int, default=2000, help="How many student users to create.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Where to write the generated benchmark fixture JSON.",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Optional deterministic run prefix. Defaults to a UTC timestamp.",
    )
    return parser


def utc_run_prefix() -> str:
    return timezone.now().strftime("bench_%Y%m%dT%H%M%SZ")


def main() -> int:
    args = build_parser().parse_args()
    run_prefix = args.prefix or utc_run_prefix()
    password_hash = make_password("bench-pass")

    teacher_username = f"{run_prefix}_teacher"
    teacher = User.objects.create(
        username=teacher_username,
        password=password_hash,
        is_staff=False,
        is_superuser=False,
    )
    UserProfile.objects.create(user=teacher, is_teacher=True)
    teacher_token = Token.objects.create(user=teacher)

    room = ExamRoom.objects.create(
        teacher=teacher,
        title=f"{run_prefix} load benchmark room",
        questions_to_assign=1,
        join_deadline=timezone.now() + timedelta(days=1),
    )
    question = ExamQuestion.objects.create(
        room=room,
        title="Square Number",
        description="Read one integer and print its square.",
        testcase_input="2\n\n3",
        expected_output="4\n\n9",
        total_marks=10,
    )

    users: list[User] = []
    for idx in range(args.users):
        users.append(
            User(
                username=f"{run_prefix}_student_{idx:04d}",
                password=password_hash,
                is_staff=False,
                is_superuser=False,
            )
        )

    with transaction.atomic():
        created_users = User.objects.bulk_create(users, batch_size=500)
        UserProfile.objects.bulk_create(
            [UserProfile(user=user, is_teacher=False) for user in created_users],
            batch_size=500,
        )
        tokens = [
            Token(key=Token.generate_key(), user=user)
            for user in created_users
        ]
        Token.objects.bulk_create(tokens, batch_size=500)
        participants = [
            RoomParticipant(room=room, student=user)
            for user in created_users
        ]
        created_participants = RoomParticipant.objects.bulk_create(participants, batch_size=500)
        through_model = RoomParticipant.assigned_questions.through
        through_model.objects.bulk_create(
            [
                through_model(roomparticipant_id=participant.id, examquestion_id=question.id)
                for participant in created_participants
            ],
            batch_size=1000,
        )

    token_by_user_id = {
        token.user_id: token.key
        for token in Token.objects.filter(user__username__startswith=f"{run_prefix}_student_").only("user_id", "key")
    }

    payload = {
        "run_prefix": run_prefix,
        "teacher": {
            "username": teacher.username,
            "token": teacher_token.key,
            "user_id": teacher.id,
        },
        "room": {
            "id": room.id,
            "code": room.room_code,
            "title": room.title,
        },
        "question": {
            "id": question.id,
            "title": question.title,
        },
        "users": [
            {
                "user_id": user.id,
                "username": user.username,
                "token": token_by_user_id[user.id],
            }
            for user in created_users
        ],
        "submission_payload": {
            "language": "python",
            "code": "n = int(input())\nprint(n * n)\n",
            "files": [
                {
                    "path": "main.py",
                    "content": "n = int(input())\nprint(n * n)\n",
                }
            ],
            "entry_file": "main.py",
            "time_limit_ms": 5000,
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"fixture_path": str(args.output), "run_prefix": run_prefix, "users": args.users}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

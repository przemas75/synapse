# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from mock import Mock

from twisted.internet import defer

from synapse import storage
from synapse.api.constants import EventTypes, Membership
from synapse.rest import admin
from synapse.rest.client.v1 import login, room

from tests import unittest

# The expected number of state events in a fresh public room.
EXPT_NUM_STATE_EVTS_IN_FRESH_PUBLIC_ROOM = 5
# The expected number of state events in a fresh private room.
EXPT_NUM_STATE_EVTS_IN_FRESH_PRIVATE_ROOM = 6


class StatsRoomTests(unittest.HomeserverTestCase):

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastore()
        self.handler = self.hs.get_stats_handler()

    def _add_background_updates(self):
        """
        Add the background updates we need to run.
        """
        # Ugh, have to reset this flag
        self.store._all_done = False

        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {"update_name": "populate_stats_prepare", "progress_json": "{}"},
            )
        )
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_process_rooms",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_prepare",
                },
            )
        )
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_process_users",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_process_rooms",
                },
            )
        )
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_cleanup",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_process_users",
                },
            )
        )

    def get_all_room_state(self):
        return self.store._simple_select_list(
            "room_stats_state", None, retcols=("name", "topic", "canonical_alias")
        )

    def _get_current_stats(self, stats_type, stat_id):
        table, id_col = storage.stats.TYPE_TO_TABLE[stats_type]

        cols = (
            ["completed_delta_stream_id"]
            + list(storage.stats.ABSOLUTE_STATS_FIELDS[stats_type])
            + list(storage.stats.PER_SLICE_FIELDS[stats_type])
        )

        return self.get_success(
            self.store._simple_select_one(
                table + "_current", {id_col: stat_id}, cols, allow_none=True
            )
        )

    def _perform_background_initial_update(self):
        # Do the initial population of the stats via the background update
        self._add_background_updates()

        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

    def test_initial_room(self):
        """
        The background updates will build the table from scratch.
        """
        r = self.get_success(self.get_all_room_state())
        self.assertEqual(len(r), 0)

        # Disable stats
        self.hs.config.stats_enabled = False
        self.handler.stats_enabled = False

        u1 = self.register_user("u1", "pass")
        u1_token = self.login("u1", "pass")

        room_1 = self.helper.create_room_as(u1, tok=u1_token)
        self.helper.send_state(
            room_1, event_type="m.room.topic", body={"topic": "foo"}, tok=u1_token
        )

        # Stats disabled, shouldn't have done anything
        r = self.get_success(self.get_all_room_state())
        self.assertEqual(len(r), 0)

        # Enable stats
        self.hs.config.stats_enabled = True
        self.handler.stats_enabled = True

        # Do the initial population of the user directory via the background update
        self._add_background_updates()

        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

        r = self.get_success(self.get_all_room_state())

        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["topic"], "foo")

    def test_initial_earliest_token(self):
        """
        Ingestion via notify_new_event will ignore tokens that the background
        update have already processed.
        """

        self.reactor.advance(86401)

        self.hs.config.stats_enabled = False
        self.handler.stats_enabled = False

        u1 = self.register_user("u1", "pass")
        u1_token = self.login("u1", "pass")

        u2 = self.register_user("u2", "pass")
        u2_token = self.login("u2", "pass")

        u3 = self.register_user("u3", "pass")
        u3_token = self.login("u3", "pass")

        room_1 = self.helper.create_room_as(u1, tok=u1_token)
        self.helper.send_state(
            room_1, event_type="m.room.topic", body={"topic": "foo"}, tok=u1_token
        )

        # Begin the ingestion by creating the temp tables. This will also store
        # the position that the deltas should begin at, once they take over.
        self.hs.config.stats_enabled = True
        self.handler.stats_enabled = True
        self.store._all_done = False
        self.get_success(self.store.update_stats_positions(None))

        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {"update_name": "populate_stats_prepare", "progress_json": "{}"},
            )
        )

        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

        # Now, before the table is actually ingested, add some more events.
        self.helper.invite(room=room_1, src=u1, targ=u2, tok=u1_token)
        self.helper.join(room=room_1, user=u2, tok=u2_token)

        # orig_delta_processor = self.store.

        # Now do the initial ingestion.
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {"update_name": "populate_stats_process_rooms", "progress_json": "{}"},
            )
        )
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_cleanup",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_process_rooms",
                },
            )
        )

        self.store._all_done = False
        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

        self.reactor.advance(86401)

        # Now add some more events, triggering ingestion. Because of the stream
        # position being set to before the events sent in the middle, a simpler
        # implementation would reprocess those events, and say there were four
        # users, not three.
        self.helper.invite(room=room_1, src=u1, targ=u3, tok=u1_token)
        self.helper.join(room=room_1, user=u3, tok=u3_token)

        # self.handler.notify_new_event()

        # We need to let the delta processor advance…
        self.pump(10 * 60)

        # Get the slices! There should be two -- day 1, and day 2.
        r = self.get_success(self.store.get_statistics_for_subject("room", room_1, 0))

        self.assertEqual(len(r), 2)

        # The oldest has 2 joined members
        self.assertEqual(r[-1]["joined_members"], 2)

        # The newest has 3
        self.assertEqual(r[0]["joined_members"], 3)

    def test_incorrect_state_transition(self):
        """
        If the state transition is not one of (JOIN, INVITE, LEAVE, BAN) to
        (JOIN, INVITE, LEAVE, BAN), an error is raised.
        """
        events = {
            "a1": {"membership": Membership.LEAVE},
            "a2": {"membership": "not a real thing"},
        }

        def get_event(event_id, allow_none=True):
            m = Mock()
            m.content = events[event_id]
            d = defer.Deferred()
            self.reactor.callLater(0.0, d.callback, m)
            return d

        def get_received_ts(event_id):
            return defer.succeed(1)

        self.store.get_received_ts = get_received_ts
        self.store.get_event = get_event

        deltas = [
            {
                "type": EventTypes.Member,
                "state_key": "some_user",
                "room_id": "room",
                "event_id": "a1",
                "prev_event_id": "a2",
                "stream_id": 60,
            }
        ]

        f = self.get_failure(self.handler._handle_deltas(deltas), ValueError)
        self.assertEqual(
            f.value.args[0], "'not a real thing' is not a valid prev_membership"
        )

        # And the other way...
        deltas = [
            {
                "type": EventTypes.Member,
                "state_key": "some_user",
                "room_id": "room",
                "event_id": "a2",
                "prev_event_id": "a1",
                "stream_id": 100,
            }
        ]

        f = self.get_failure(self.handler._handle_deltas(deltas), ValueError)
        self.assertEqual(
            f.value.args[0], "'not a real thing' is not a valid membership"
        )

    def test_redacted_prev_event(self):
        """
        If the prev_event does not exist, then it is assumed to be a LEAVE.
        """
        u1 = self.register_user("u1", "pass")
        u1_token = self.login("u1", "pass")

        room_1 = self.helper.create_room_as(u1, tok=u1_token)

        # Do the initial population of the stats via the background update
        self._add_background_updates()

        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

        events = {"a1": None, "a2": {"membership": Membership.JOIN}}

        def get_event(event_id, allow_none=True):
            if events.get(event_id):
                m = Mock()
                m.content = events[event_id]
            else:
                m = None
            d = defer.Deferred()
            self.reactor.callLater(0.0, d.callback, m)
            return d

        def get_received_ts(event_id):
            return defer.succeed(1)

        self.store.get_received_ts = get_received_ts
        self.store.get_event = get_event

        deltas = [
            {
                "type": EventTypes.Member,
                "state_key": "some_user:test",
                "room_id": room_1,
                "event_id": "a2",
                "prev_event_id": "a1",
                "stream_id": 100,
            }
        ]

        # Handle our fake deltas, which has a user going from LEAVE -> JOIN.
        self.get_success(self.handler._handle_deltas(deltas))

        # One delta, with two joined members -- the room creator, and our fake
        # user.
        r = self.get_success(self.store.get_statistics_for_subject("room", room_1, 0))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["joined_members"], 2)

    def test_create_user(self):
        """
        When we create a user, it should have statistics already ready.
        """

        u1 = self.register_user("u1", "pass")

        u1stats = self._get_current_stats("user", u1)

        self.assertIsNotNone(u1stats)

        # row is complete
        self.assertIsNotNone(u1stats["completed_delta_stream_id"])

        # not in any rooms by default
        self.assertEqual(u1stats["public_rooms"], 0)
        self.assertEqual(u1stats["private_rooms"], 0)

    def test_create_room(self):
        """
        When we create a room, it should have statistics already ready.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)
        r1stats = self._get_current_stats("room", r1)
        r2 = self.helper.create_room_as(u1, tok=u1token, is_public=False)
        r2stats = self._get_current_stats("room", r2)

        self.assertIsNotNone(r1stats)
        self.assertIsNotNone(r2stats)

        # row is complete
        self.assertIsNotNone(r1stats["completed_delta_stream_id"])
        self.assertIsNotNone(r2stats["completed_delta_stream_id"])

        # contains the default things you'd expect in a fresh room
        self.assertEqual(
            r1stats["total_events"],
            EXPT_NUM_STATE_EVTS_IN_FRESH_PUBLIC_ROOM,
            "Wrong number of total_events in new room's stats!"
            " You may need to update this if more state events are added to"
            " the room creation process.",
        )
        self.assertEqual(
            r2stats["total_events"],
            EXPT_NUM_STATE_EVTS_IN_FRESH_PRIVATE_ROOM,
            "Wrong number of total_events in new room's stats!"
            " You may need to update this if more state events are added to"
            " the room creation process.",
        )

        self.assertEqual(
            r1stats["current_state_events"], EXPT_NUM_STATE_EVTS_IN_FRESH_PUBLIC_ROOM
        )
        self.assertEqual(
            r2stats["current_state_events"], EXPT_NUM_STATE_EVTS_IN_FRESH_PRIVATE_ROOM
        )

        self.assertEqual(r1stats["joined_members"], 1)
        self.assertEqual(r1stats["invited_members"], 0)
        self.assertEqual(r1stats["banned_members"], 0)

        self.assertEqual(r2stats["joined_members"], 1)
        self.assertEqual(r2stats["invited_members"], 0)
        self.assertEqual(r2stats["banned_members"], 0)

    def test_send_message_increments_total_events(self):
        """
        When we send a message, it increments total_events.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)
        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.send(r1, "hiss", tok=u1token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)

    def test_send_state_event_nonoverwriting(self):
        """
        When we send a non-overwriting state event, it increments total_events AND current_state_events
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        self.helper.send_state(
            r1, "cat.hissing", {"value": True}, tok=u1token, state_key="tabby"
        )

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.send_state(
            r1, "cat.hissing", {"value": False}, tok=u1token, state_key="moggy"
        )

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            1,
        )

    def test_send_state_event_overwriting(self):
        """
        When we send an overwriting state event, it increments total_events ONLY
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        self.helper.send_state(
            r1, "cat.hissing", {"value": True}, tok=u1token, state_key="tabby"
        )

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.send_state(
            r1, "cat.hissing", {"value": False}, tok=u1token, state_key="tabby"
        )

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            0,
        )

    def test_join_first_time(self):
        """
        When a user joins a room for the first time, total_events, current_state_events and
        joined_members should increase by exactly 1.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        u2 = self.register_user("u2", "pass")
        u2token = self.login("u2", "pass")

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.join(r1, u2, tok=u2token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            1,
        )
        self.assertEqual(
            r1stats_post["joined_members"] - r1stats_ante["joined_members"], 1
        )

    def test_join_after_leave(self):
        """
        When a user joins a room after being previously left, total_events and
        joined_members should increase by exactly 1.
        current_state_events should not increase.
        left_members should decrease by exactly 1.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        u2 = self.register_user("u2", "pass")
        u2token = self.login("u2", "pass")

        self.helper.join(r1, u2, tok=u2token)
        self.helper.leave(r1, u2, tok=u2token)

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.join(r1, u2, tok=u2token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            0,
        )
        self.assertEqual(
            r1stats_post["joined_members"] - r1stats_ante["joined_members"], +1
        )
        self.assertEqual(
            r1stats_post["left_members"] - r1stats_ante["left_members"], -1
        )

    def test_invited(self):
        """
        When a user invites another user, current_state_events, total_events and
        invited_members should increase by exactly 1.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        u2 = self.register_user("u2", "pass")

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.invite(r1, u1, u2, tok=u1token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            1,
        )
        self.assertEqual(
            r1stats_post["invited_members"] - r1stats_ante["invited_members"], +1
        )

    def test_join_after_invite(self):
        """
        When a user joins a room after being invited, total_events and
        joined_members should increase by exactly 1.
        current_state_events should not increase.
        invited_members should decrease by exactly 1.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        u2 = self.register_user("u2", "pass")
        u2token = self.login("u2", "pass")

        self.helper.invite(r1, u1, u2, tok=u1token)

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.join(r1, u2, tok=u2token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            0,
        )
        self.assertEqual(
            r1stats_post["joined_members"] - r1stats_ante["joined_members"], +1
        )
        self.assertEqual(
            r1stats_post["invited_members"] - r1stats_ante["invited_members"], -1
        )

    def test_left(self):
        """
        When a user leaves a room after joining, total_events and
        left_members should increase by exactly 1.
        current_state_events should not increase.
        joined_members should decrease by exactly 1.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        u2 = self.register_user("u2", "pass")
        u2token = self.login("u2", "pass")

        self.helper.join(r1, u2, tok=u2token)

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.leave(r1, u2, tok=u2token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            0,
        )
        self.assertEqual(
            r1stats_post["left_members"] - r1stats_ante["left_members"], +1
        )
        self.assertEqual(
            r1stats_post["joined_members"] - r1stats_ante["joined_members"], -1
        )

    def test_banned(self):
        """
        When a user is banned from a room after joining, total_events and
        left_members should increase by exactly 1.
        current_state_events should not increase.
        banned_members should decrease by exactly 1.
        """

        self._perform_background_initial_update()

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        u2 = self.register_user("u2", "pass")
        u2token = self.login("u2", "pass")

        self.helper.join(r1, u2, tok=u2token)

        r1stats_ante = self._get_current_stats("room", r1)

        self.helper.change_membership(r1, u1, u2, "ban", tok=u1token)

        r1stats_post = self._get_current_stats("room", r1)

        self.assertEqual(r1stats_post["total_events"] - r1stats_ante["total_events"], 1)
        self.assertEqual(
            r1stats_post["current_state_events"] - r1stats_ante["current_state_events"],
            0,
        )
        self.assertEqual(
            r1stats_post["banned_members"] - r1stats_ante["banned_members"], +1
        )
        self.assertEqual(
            r1stats_post["joined_members"] - r1stats_ante["joined_members"], -1
        )

    def test_initial_background_update(self):
        """
        Test that statistics can be generated by the initial background update
        handler.

        This test also tests that stats rows are not created for new subjects
        when stats are disabled. However, it may be desirable to change this
        behaviour eventually to still keep current rows.
        """

        self.hs.config.stats_enabled = False

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token)

        # test that these subjects, which were created during a time of disabled
        # stats, do not have stats.
        self.assertIsNone(self._get_current_stats("room", r1))
        self.assertIsNone(self._get_current_stats("user", u1))

        self.hs.config.stats_enabled = True

        self._perform_background_initial_update()

        r1stats = self._get_current_stats("room", r1)
        u1stats = self._get_current_stats("user", u1)

        self.assertIsNotNone(r1stats["completed_delta_stream_id"])
        self.assertIsNotNone(u1stats["completed_delta_stream_id"])

        self.assertEqual(r1stats["joined_members"], 1)
        self.assertEqual(
            r1stats["total_events"], EXPT_NUM_STATE_EVTS_IN_FRESH_PUBLIC_ROOM
        )
        self.assertEqual(
            r1stats["current_state_events"], EXPT_NUM_STATE_EVTS_IN_FRESH_PUBLIC_ROOM
        )

        self.assertEqual(u1stats["public_rooms"], 1)

    def test_incomplete_stats(self):
        """
        This tests that we track incomplete statistics.

        We first test that incomplete stats are incrementally generated,
        following the preparation of a background regen.

        We then test that these incomplete rows are completed by the background
        regen.
        """

        u1 = self.register_user("u1", "pass")
        u1token = self.login("u1", "pass")
        u2 = self.register_user("u2", "pass")
        u2token = self.login("u2", "pass")
        u3 = self.register_user("u3", "pass")
        r1 = self.helper.create_room_as(u1, tok=u1token, is_public=False)

        # preparation stage of the initial background update
        # Ugh, have to reset this flag
        self.store._all_done = False

        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {"update_name": "populate_stats_prepare", "progress_json": "{}"},
            )
        )

        self.get_success(
            self.store._simple_delete(
                "room_stats_current", {"1": 1}, "test_delete_stats"
            )
        )
        self.get_success(
            self.store._simple_delete(
                "user_stats_current", {"1": 1}, "test_delete_stats"
            )
        )

        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

        r1stats_ante = self._get_current_stats("room", r1)
        u1stats_ante = self._get_current_stats("user", u1)
        u2stats_ante = self._get_current_stats("user", u2)

        self.helper.invite(r1, u1, u2, tok=u1token)
        self.helper.join(r1, u2, tok=u2token)
        self.helper.invite(r1, u1, u3, tok=u1token)
        self.helper.send(r1, "thou shalt yield", tok=u1token)

        r1stats_post = self._get_current_stats("room", r1)
        u1stats_post = self._get_current_stats("user", u1)
        u2stats_post = self._get_current_stats("user", u2)

        # now let the background update continue & finish

        self.store._all_done = False
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_process_rooms",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_prepare",
                },
            )
        )
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_process_users",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_process_rooms",
                },
            )
        )
        self.get_success(
            self.store._simple_insert(
                "background_updates",
                {
                    "update_name": "populate_stats_cleanup",
                    "progress_json": "{}",
                    "depends_on": "populate_stats_process_users",
                },
            )
        )

        while not self.get_success(self.store.has_completed_background_updates()):
            self.get_success(self.store.do_next_background_update(100), by=0.1)

        r1stats_complete = self._get_current_stats("room", r1)
        u1stats_complete = self._get_current_stats("user", u1)
        u2stats_complete = self._get_current_stats("user", u2)

        # now we make our assertions

        # first check that none of the stats rows were complete before
        # the background update occurred.
        self.assertIsNone(r1stats_ante["completed_delta_stream_id"])
        self.assertIsNone(r1stats_post["completed_delta_stream_id"])
        self.assertIsNone(u1stats_ante["completed_delta_stream_id"])
        self.assertIsNone(u1stats_post["completed_delta_stream_id"])
        self.assertIsNone(u2stats_ante["completed_delta_stream_id"])
        self.assertIsNone(u2stats_post["completed_delta_stream_id"])

        # check that _ante rows are all skeletons without any deltas applied
        self.assertEqual(r1stats_ante["joined_members"], 0)
        self.assertEqual(r1stats_ante["invited_members"], 0)
        self.assertEqual(r1stats_ante["total_events"], 0)
        self.assertEqual(r1stats_ante["current_state_events"], 0)

        self.assertEqual(u1stats_ante["public_rooms"], 0)
        self.assertEqual(u1stats_ante["private_rooms"], 0)
        self.assertEqual(u2stats_ante["public_rooms"], 0)
        self.assertEqual(u2stats_ante["private_rooms"], 0)

        # check that _post rows have the expected deltas applied
        self.assertEqual(r1stats_post["joined_members"], 1)
        self.assertEqual(r1stats_post["invited_members"], 1)
        self.assertEqual(r1stats_post["total_events"], 4)
        self.assertEqual(r1stats_post["current_state_events"], 2)

        self.assertEqual(u1stats_post["public_rooms"], 0)
        self.assertEqual(u1stats_post["private_rooms"], 0)
        self.assertEqual(u2stats_post["public_rooms"], 0)
        self.assertEqual(u2stats_post["private_rooms"], 1)

        # check that _complete rows are complete and correct
        self.assertEqual(r1stats_complete["joined_members"], 2)
        self.assertEqual(r1stats_complete["invited_members"], 1)
        self.assertEqual(
            r1stats_complete["total_events"],
            4 + EXPT_NUM_STATE_EVTS_IN_FRESH_PRIVATE_ROOM,
        )
        self.assertEqual(
            r1stats_complete["current_state_events"],
            2 + EXPT_NUM_STATE_EVTS_IN_FRESH_PRIVATE_ROOM,
        )

        self.assertEqual(u1stats_complete["public_rooms"], 0)
        self.assertEqual(u1stats_complete["private_rooms"], 1)
        self.assertEqual(u2stats_complete["public_rooms"], 0)
        self.assertEqual(u2stats_complete["private_rooms"], 1)
"""Whole-pose integrity: connected hands, body participation, no shrinking rig."""
from collections import deque
from naiwa.whole_motion import key, put_down, ground_prop
from naiwa.motion import MotionPlayer
from naiwa.sprite_defs import HAND,HAND_LIGHT,HAND_DARK,SHADOW


def component(frame):
    remaining={(x,y) for y,row in enumerate(frame) for x,p in enumerate(row) if p[3]==255}
    groups=[]
    while remaining:
        seed=remaining.pop();seen={seed};queue=deque([seed])
        while queue:
            x,y=queue.popleft()
            for dx,dy in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                nxt=(x+dx,y+dy)
                if nxt in remaining:
                    remaining.remove(nxt);seen.add(nxt);queue.append(nxt)
        groups.append(seen)
    return max(groups,key=len)


def test_every_illustrated_pose_has_hands_connected_to_the_complete_actor():
    for kind in ("book","keyboard","notebook","image","signals","rising"):
        for index in range(16):
            frame=key(kind,index);body=component(frame)
            assert len(body)>5000,(kind,index)
            palms={(x,y) for y,row in enumerate(frame[:160]) for x,p in enumerate(row)
                   if p in {HAND,HAND_LIGHT,HAND_DARK}}
            assert palms and len(palms-body)<8,(kind,index,len(palms-body))
            assert not any(frame[y][x][3]==255 for y in range(len(frame))
                           for x in (0,1,158,159)),(kind,index,"clipped figure")


def test_prop_moves_in_full_poses_while_body_crouches_then_rises():
    for kind in ("book","keyboard","notebook","image"):
        frames=put_down(kind)
        tops=[min(y for y,row in enumerate(f) if any(p[3]==255 for p in row)) for f in frames]
        assert max(tops)-min(tops)>15,kind
        assert frames[0]!=frames[-1]
        assert all(any(p==SHADOW for row in f for p in row) for f in frames)


def test_actual_player_retains_the_placed_object_after_return_to_thinking():
    for variant,kind in enumerate(("book","keyboard","notebook","image")):
        player=MotionPlayer();player.sample("tool",0,variant);player.sample("tool",2,variant)
        for tick in range(100):
            frame,_=player.sample("working",2.5+tick/25)
        assert player._physical is None and player._ground_kind==kind
        prop=ground_prop(kind)
        from naiwa.motion import animated_frame
        body=animated_frame("working",int((2.5+99/25-player.since)*25)%80)
        assert all(frame[y][x]==p for y,row in enumerate(prop) for x,p in enumerate(row)
                   if p[3] and body[y][x][3]==0)


def test_thinking_preserves_the_complete_drawn_feet_and_ground_objects_stay_behind_actor():
    from naiwa.whole_motion import thinking,with_ground
    actor=thinking()
    assert actor==key("rising",15)
    for kind in ("book","keyboard","notebook","image"):
        frame=with_ground(actor,kind)
        assert all(frame[y][x]==p for y,row in enumerate(actor) for x,p in enumerate(row) if p[3]==255)


def test_production_contains_no_segmented_rig_or_shrinking_relay():
    from pathlib import Path
    import naiwa.motion as motion
    assert not hasattr(__import__("naiwa.rigid_motion",fromlist=["RigidRelay"]),"RigidRelay")
    assert not (Path(motion.__file__).parent/"articulated_motion.py").exists()

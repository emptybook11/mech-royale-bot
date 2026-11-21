"""A placement-first bot for the official Mech Royale simulator."""

from collections import deque
import math

import mechfight as mf
from arena import Arena, delta, unit
from combat import aim_for, aligned, dimensions


class Bot:
    name = 'Cover Circuit'
    WEAPON_VALUE = {'gun': 0, 'mortar': 1, 'lightning': 2, 'laser': 3}
    UPGRADE_VALUE = {
        'nanites': 12, 'servos': 10, 'armor': 9.5, 'plating': 9,
        'gun_mag': 8.5, 'jets': 7.5, 'gun_damage': 7, 'laser_charge': 7,
        'reactor': 6.5, 'gun_rate': 6.5, 'gun_velocity': 6, 'capacitor': 5,
        'laser_damage': 6, 'lightning_efficiency': 6, 'lightning_range': 6,
        'gyros': 5, 'sensors': 4.5, 'mortar_reload': 4, 'mortar_splash': 4,
    }

    def __init__(self, hello):
        self.config = hello['config']
        self.arena = Arena(hello['map']['dir'], self.config['mech'])
        self.tick_seconds = 1 / self.config['match']['tick_hz']
        self.damage = deque()
        self.motion = deque()
        self.path = deque()
        self.route_goal = None
        self.route_until = 0.0
        self.cover_goal = None
        self.cover_until = 0.0
        self.retreat_until = 0.0
        self.recover_until = 0.0
        self.boosting = False
        self.last_move = (0.0, 0.0)
        self.target_id = None
        self.crate_id = None
        self.crate_until = 0.0
        self.crate_retry = {}
        self.turn = 1 if hello['slot'] % 2 == 0 else -1

    @staticmethod
    def zone_margin(point, center, radius):
        return radius - math.dist(point[:2], center)

    def zone_goal(self, obs):
        me, zone, now = obs['self'], obs['zone'], obs['t']
        pos, center, radius = me['pos'], zone['next_center'], zone['next_radius']
        if now >= zone['shrink_end']:
            center, radius = zone['center'], zone['radius']
        margin = self.zone_margin(pos, center, radius)
        current_margin = self.zone_margin(pos, zone['center'], zone['radius'])
        buffer = min(32.0, radius * 0.48)
        eta = max(0.0, buffer - margin) / max(1, me['stats']['walk_speed'])
        urgent = (not me['in_zone'] or current_margin < min(12, zone['radius'] * 0.3)
                  or (margin < buffer and zone['shrink_end'] - now < eta * 1.55 + 11))
        ux, uy = unit(pos[0] - center[0], pos[1] - center[1])
        depth = max(0.0, radius - (max(buffer + 18, radius * 0.30) if not urgent else buffer + 10))
        return (center[0] + ux * depth, center[1] + uy * depth), urgent

    def open_ground(self, obj, me):
        x, y, z = obj['pos']
        return (abs(z - self.arena.ground(x, y)) < 3.5
                or (abs(z - me['pos'][2]) < 3.5 and not me['grounded']))

    def loot_goal(self, obs, enemy_distance):
        me, zone = obs['self'], obs['zone']
        pos = me['pos']
        weapons = me['weapons']
        equipped = max(self.WEAPON_VALUE[w['kind']] for w in weapons if w)
        held = {w['kind'] for w in weapons if w}
        candidates = []
        for cube in obs['cubes']:
            if self.open_ground(cube, me):
                distance = math.dist(pos[:2], cube['pos'][:2])
                if distance <= (60 if enemy_distance > 55 else 17):
                    candidates.append((distance - 41, 'cube', cube))
        for pickup in obs['pickups']:
            value = self.WEAPON_VALUE[pickup['kind']]
            if value <= equipped or pickup['kind'] in held or not self.open_ground(pickup, me):
                continue
            distance = math.dist(pos[:2], pickup['pos'][:2])
            limit = (67 if equipped == 0 else 38) if enemy_distance > 65 else 11
            if distance <= limit:
                candidates.append((distance - 16 - value * 5, 'weapon', pickup))
        for drop in obs.get('drops', ()):
            if enemy_distance < 64 or drop['holder'] not in (None, me['id']):
                continue
            distance = math.dist(pos[:2], drop['pos'][:2])
            if distance <= 105 and drop['lands_in'] < distance / 10 + 2.5:
                healing = 39 * (1 - me['hp'] / me['max_hp'])
                candidates.append((distance - 72 - healing - drop['progress'] * 9, 'drop', drop))
        if enemy_distance > 86 and me['cubes'] < 6:
            for crate in obs['crates']:
                distance = math.dist(pos[:2], crate['pos'][:2])
                if (distance <= 64 and self.open_ground(crate, me)
                        and self.crate_retry.get(crate['id'], -math.inf) <= obs['t']):
                    focus = 16 if self.crate_id == crate['id'] else 0
                    candidates.append((distance + 8 - focus, 'crate', crate))
        permitted = ((score, kind, obj) for score, kind, obj in candidates
                     if self.zone_margin(obj['pos'], zone['center'], zone['radius']) > 3)
        return min(permitted, key=lambda item: item[0], default=(0, None, None))[1:]

    def loot_action(self, obs, action):
        me = obs['self']
        slots = me['weapons']
        held = {weapon['kind'] for weapon in slots if weapon}
        radius = self.config['loot']['pickup_radius']
        candidates = [pickup for pickup in obs['pickups']
                      if pickup['kind'] not in held and abs(pickup['pos'][2] - me['pos'][2]) <= 4
                      and math.dist(pickup['pos'][:2], me['pos'][:2]) <= radius]
        if candidates:
            pickup = max(candidates, key=lambda item: self.WEAPON_VALUE[item['kind']])
            if None in slots and pickup['kind'] != 'gun':
                action['take'] = pickup['id']
            elif None not in slots:
                secondary = next((i for i, weapon in enumerate(slots) if weapon['kind'] != 'gun'), None)
                if secondary is not None:
                    weapon = slots[secondary]
                    if self.WEAPON_VALUE[pickup['kind']] > self.WEAPON_VALUE[weapon['kind']] and not weapon.get('charge', 0):
                        action.update(take=pickup['id'], drop=secondary)
        if obs['offer']:
            choices = obs['offer']['choices']
            value = self.UPGRADE_VALUE
            action['pick'] = max(range(len(choices)), key=lambda i: value.get(choices[i]['id'], 3))

    def update_damage(self, obs):
        now = obs['t']
        for event in obs['events']:
            if event['type'] == 'damaged' and event['weapon'] != 'zone':
                self.damage.append((now, event['amount'], event['bearing'], event['by']))
        while self.damage and self.damage[0][0] < now - 2.5:
            self.damage.popleft()

    def retreat_goal(self, obs, enemies, zone_goal):
        me, now, zone = obs['self'], obs['t'], obs['zone']
        pos = me['pos']
        if self.cover_goal is not None and now < self.cover_until:
            return self.cover_goal
        candidates = [(pos[0], pos[1]), zone_goal]
        for index in range(12):
            yaw = index * math.tau / 12
            for distance in (15, 36):
                candidates.append((pos[0] + distance * math.cos(yaw), pos[1] + distance * math.sin(yaw)))
        threats = enemies[:2]
        damage_bearing = self.damage[-1][2] if self.damage else None
        if not threats and damage_bearing is not None:
            threats = [{'pos': [pos[0] + 85 * math.cos(damage_bearing),
                                pos[1] + 85 * math.sin(damage_bearing), pos[2]]}]
        origins = [(actor['pos'][0], actor['pos'][1], actor['pos'][2] + 7) for actor in threats]
        danger_margin = min(9, zone['radius'] * 0.25)
        best, best_score = zone_goal, -math.inf
        for x, y in candidates:
            margin = self.zone_margin((x, y), zone['center'], zone['radius'])
            if margin < danger_margin or self.arena.grid[self.arena.cell((x, y))]:
                continue
            distance = math.dist((x, y), pos[:2])
            ground = self.arena.ground(x, y)
            covered = sum(not self.arena.ray(origin, (x, y, ground + 5.5)) for origin in origins)
            separation = min((math.dist((x, y), origin[:2]) for origin in origins), default=0)
            score = 54 * covered + 0.38 * separation - 0.45 * distance
            if origins and covered < len(origins) and separation < 40:
                score -= (40 - separation) * 1.5
            if distance and not self.arena.direct(pos, (x, y)):
                score -= 12
            if score > best_score:
                best, best_score = (x, y), score
        self.cover_goal, self.cover_until = best, now + 0.9
        return best

    def combat_target(self, obs, enemies, crate, retreating):
        me, pos = obs['self'], obs['self']['pos']
        weapons = me['weapons']
        if retreating:
            return None, None, None
        ranked = sorted(enemies, key=lambda actor:
                        math.dist(pos[:2], actor['pos'][:2])
                        + 0.025 * actor.get('hp', actor.get('max_hp', 1400))
                        - (13 if actor['id'] == self.target_id else 0))
        if crate is not None:
            ranked.append(crate)
        drones = [actor for actor in obs['mechs'] if actor['kind'] == 'npc'
                  and math.dist(pos[:2], actor['pos'][:2]) < 42]
        nearest_drone = min(drones, key=lambda actor: math.dist(pos[:2], actor['pos'][:2]), default=None)
        if nearest_drone is not None:
            ranked.append(nearest_drone)
        for target in ranked[:4]:
            distance = math.dist(pos[:2], target['pos'][:2])
            if distance > 170 or (target.get('kind') not in ('mech', 'npc') and distance > 60):
                continue
            order = []
            for index, weapon in enumerate(weapons):
                if not weapon:
                    continue
                kind = weapon['kind']
                ready = weapon['cooldown'] <= 0.11
                if kind == 'laser' and weapon.get('charge', 0) > 0:
                    priority = 8
                elif kind == 'laser' and ready and (distance > 29 or not target.get('kind')):
                    priority = 5
                elif kind == 'lightning' and ready and me['energy'] >= weapon['stats']['energy'] + 7:
                    priority = 4
                elif kind == 'mortar' and ready and target.get('kind') == 'mech':
                    priority = 3
                elif kind == 'gun':
                    priority = 2
                else:
                    continue
                order.append((priority, index, weapon))
            for _, index, weapon in sorted(order, reverse=True):
                solution = aim_for(self.arena, me, target, weapon, self.config, self.tick_seconds)
                if solution is not None:
                    return target, index, solution
        return None, None, None

    def fire_control(self, obs, action, target, index, solution):
        if target is None:
            self.target_id = None
            return
        me = obs['self']
        weapon = me['weapons'][index]
        stats = weapon['stats']
        self.target_id = target['id']
        action['aim'] = [solution.yaw, solution.pitch]
        ready = aligned(me, solution, self.tick_seconds)
        kind = weapon['kind']
        if kind == 'laser':
            charge = weapon.get('charge', 0)
            release = max(stats['min_charge'], stats['charge'] - 0.045)
            if solution.distance < 35:
                release = max(stats['min_charge'], stats['charge'] * 0.65)
            action['fire'][index] = (charge < release or not ready) if charge else (
                weapon['cooldown'] <= 0.06 and aligned(me, solution, 0.30))
        elif kind == 'lightning':
            action['fire'][index] = abs(delta(solution.yaw, me['aim'][0])) <= math.radians(stats['cone_deg'] * 0.46)
        else:
            action['fire'][index] = ready

        if kind in ('laser', 'lightning'):
            gun_index = next((i for i, w in enumerate(me['weapons']) if w and w['kind'] == 'gun'), None)
            if gun_index is not None and (kind == 'lightning' or not action['fire'][index]):
                gun = me['weapons'][gun_index]
                gun_aim = aim_for(self.arena, me, target, gun, self.config, self.tick_seconds)
                if gun_aim and aligned(me, gun_aim, 0.0):
                    # Both weapons share the same torso. Fire together only when the lead angles agree.
                    radius, _ = dimensions(target, self.config)
                    allowance = math.atan2(radius * 0.55, max(1.0, gun_aim.distance))
                    if math.hypot(delta(gun_aim.yaw, solution.yaw), gun_aim.pitch - solution.pitch) < allowance:
                        action['fire'][gun_index] = True

    def waypoint(self, obs, goal):
        pos, now = obs['self']['pos'], obs['t']
        if self.arena.direct(pos, goal):
            self.path.clear()
            self.route_goal = None
            return goal
        if self.route_goal is None or math.dist(goal, self.route_goal) > 11 or now >= self.route_until:
            self.path = deque(self.arena.route(pos, goal))
            self.route_goal, self.route_until = goal, now + 0.9
        while self.path and math.dist(pos[:2], self.path[0]) < 4.0:
            self.path.popleft()
        if self.path:
            for waypoint in reversed(list(self.path)[:5]):
                if self.arena.direct(pos, waypoint):
                    return waypoint
            return self.path[0]
        return goal

    def hazards(self, obs):
        pos = obs['self']['pos']
        nearby = []
        for shot in obs['projectiles']:
            dx, dy, dz = (shot['pos'][i] - pos[i] for i in range(3))
            vx, vy, vz = shot['vel']
            horizontal = math.hypot(vx, vy)
            if math.hypot(dx, dy) > horizontal * 0.8 + 15:
                continue
            eta = -(dx * vx + dy * vy) / max(1.0, horizontal * horizontal)
            gap = math.hypot(dx + eta * vx, dy + eta * vy)
            if shot['kind'] == 'mortar' or (-0.05 < eta < 0.75 and gap < 16):
                nearby.append((gap, (dx, dy, dz, vx, vy, vz, shot['kind'] == 'mortar')))
        nearby.sort(key=lambda shot: shot[0])
        return [shot for _, shot in nearby[:12]]

    def projectile_penalty(self, shots, move, speed, stats):
        penalty = 0.0
        for dx, dy, z, vx, vy, vz, mortar in shots:
            rx, ry = vx - speed * move[0], vy - speed * move[1]
            time = -(dx * rx + dy * ry) / max(1.0, rx * rx + ry * ry)
            if not 0 < time < 0.64:
                continue
            height = z + time * vz
            if mortar:
                height -= 0.5 * self.config['weapons']['mortar']['gravity'] * time * time
            if not -2 < height < stats['height'] + 2:
                continue
            gap = math.hypot(dx + time * rx, dy + time * ry)
            radius = stats['radius'] + (7 if mortar else 1.1)
            penalty += max(0.0, (radius - gap) / radius) * (7.0 if mortar else 3.5)
        return penalty

    def steer(self, obs, desired, running, enemies, hold):
        me, zone = obs['self'], obs['zone']
        x, y, z = me['pos']
        stats = me['stats']
        speed = stats['boost_speed'] if running and me['grounded'] else stats['walk_speed']
        shots = self.hazards(obs)
        yaw = math.atan2(desired[1], desired[0])
        if math.hypot(*desired) < 0.01:
            if not shots:
                return 0.0, 0.0
            yaw = me['aim'][0]
        # Pick nearby headings before committing to them. The route handles larger detours.
        candidates = [(math.cos(yaw + offset), math.sin(yaw + offset))
                      for offset in (0, 0.35, -0.35, 0.7, -0.7, 1.15, -1.15, 1.57, -1.57, 2.25, -2.25, math.pi)]
        candidates.append((0.0, 0.0))
        current_margin = self.zone_margin((x, y), zone['center'], zone['radius'])
        start_ground = self.arena.ground(x, y)
        threats = [enemy['pos'][:2] for enemy in enemies[:2] if math.dist((x, y), enemy['pos'][:2]) < 60]
        best, best_score = (0.0, 0.0), -math.inf
        for mx, my in candidates:
            score = 2.6 * (mx * desired[0] + my * desired[1])
            score += 0.32 * (mx * self.last_move[0] + my * self.last_move[1])
            if not (mx or my):
                score += 1.0 if hold else -1.1
            px, py = x + mx * speed * 0.55, y + my * speed * 0.55
            margin = self.zone_margin((px, py), zone['center'], zone['radius'])
            if margin < min(5, current_margin):
                score -= 0.85 * (min(5, current_margin) - margin)
            if current_margin < 0:
                score += 0.26 * (margin - current_margin)
            for fraction in (0.35, 0.68, 1.0) if mx or my else ():
                qx, qy = x + fraction * (px - x), y + fraction * (py - y)
                floor = self.arena.ground(qx, qy)
                feet = max(z, floor)
                allowed_rise = max(stats['step_up'], math.dist((x, y), (qx, qy)) * 0.68)
                if (floor > max(z, start_ground) + allowed_rise
                        or not self.arena.free(qx, qy, feet, stats['radius'] + 0.15, stats['height'], stats['step_up'])):
                    score -= 8 * (1.5 - fraction)
                    break
            for threat in threats:
                old_gap = math.dist((x, y), threat)
                new_gap = math.dist((px, py), threat)
                if new_gap < min(old_gap, 29):
                    score -= 0.18 * (min(old_gap, 29) - new_gap)
            score -= self.projectile_penalty(shots, (mx, my), speed, stats)
            if score > best_score:
                best, best_score = (mx, my), score
        return best

    def act(self, obs):
        me, now, zone = obs['self'], obs['t'], obs['zone']
        pos, stats = me['pos'], me['stats']
        hp = me['hp'] / max(1, me['max_hp'])
        enemies = sorted((actor for actor in obs['mechs'] if actor['kind'] == 'mech'),
                         key=lambda actor: math.dist(pos[:2], actor['pos'][:2]))
        nearest = math.dist(pos[:2], enemies[0]['pos'][:2]) if enemies else math.inf
        close_count = sum(math.dist(pos[:2], enemy['pos'][:2]) < 63 for enemy in enemies)
        self.update_damage(obs)
        damage = sum(event[1] for event in self.damage)
        if ((hp < 0.59 and (nearest < 115 or damage > 0))
                or (close_count >= 2 and damage > 0.085 * me['max_hp'] and hp < 0.88)
                or (hp < 0.46 and now < self.retreat_until)):
            self.retreat_until = now + 2.6
        retreating = now < self.retreat_until
        zone_goal, urgent = self.zone_goal(obs)
        kind, loot = self.loot_goal(obs, nearest) if not retreating and not urgent else (None, None)
        crate = loot if kind == 'crate' else None
        if crate and self.crate_id != crate['id']:
            self.crate_id, self.crate_until = crate['id'], now + 6.0
        if crate and now > self.crate_until:
            self.crate_retry[crate['id']] = now + 8.0
            self.crate_id, crate, kind = None, None, None
        if kind != 'crate':
            self.crate_id = None

        goal, hold = zone_goal, False
        target, slot, solution = self.combat_target(obs, enemies, crate, retreating and not urgent)
        if urgent:
            mode = 'rotate'
        elif retreating:
            mode = 'recover'
            goal = self.retreat_goal(obs, enemies, zone_goal)
            hold = math.dist(pos[:2], goal) < 4.3
        elif kind and kind != 'crate':
            mode = kind
            goal = tuple(loot['pos'][:2])
            hold = kind == 'drop' and math.dist(pos[:2], goal) < loot['radius'] * 0.60
        elif target is not None and target.get('kind') == 'mech':
            mode = 'engage'
            vx, vy = unit(target['pos'][0] - pos[0], target['pos'][1] - pos[1])
            distance = math.dist(pos[:2], target['pos'][:2])
            preferred = 39 if target.get('hp', me['max_hp']) < 0.5 * me['max_hp'] else 57
            if any(weapon and weapon['kind'] == 'lightning' for weapon in me['weapons']):
                preferred -= 9
            radial = max(-1.0, min(0.62, (distance - preferred) / 32))
            sideways = self.turn * (-1 if int(now / 4.2) % 2 else 1)
            gx, gy = unit(vx * radial - vy * sideways, vy * radial + vx * sideways)
            goal = pos[0] + gx * 14, pos[1] + gy * 14
        elif kind == 'crate':
            mode = 'crate'
            goal = tuple(crate['pos'][:2])
            hold = target is crate and math.dist(pos[:2], goal) < 27
        else:
            mode = 'patrol'
            if math.dist(pos[:2], goal) < 13:
                cx, cy = zone['next_center']
                yaw = now * 0.16 + self.turn * 1.7
                radius = min(25, zone['next_radius'] * 0.35)
                goal = cx + radius * math.cos(yaw), cy + radius * math.sin(yaw)
                hold = math.dist(pos[:2], goal) < 3

        goal_distance = math.dist(pos[:2], goal)
        if hold or goal_distance < 0.9:
            desired = (0.0, 0.0)
        else:
            nx, ny = self.waypoint(obs, goal) if mode != 'engage' else goal
            desired = unit(nx - pos[0], ny - pos[1])
        running = ((urgent or retreating and not hold or mode == 'engage' and nearest < 30)
                   and me['energy'] > (12 if self.boosting or not me['in_zone'] else 46))
        move = self.steer(obs, desired, running, enemies, hold)
        if goal_distance < 5 and mode in ('drop', 'cube', 'weapon') and not hold:
            scale = min(1.0, max(0.22, goal_distance / 4.8))
            move = (move[0] * scale, move[1] * scale)

        self.motion.append((now, pos[0], pos[1]))
        while self.motion and self.motion[0][0] < now - 0.8:
            self.motion.popleft()
        oldest = self.motion[0]
        stuck = (not hold and goal_distance > 11 and mode != 'engage' and now - oldest[0] > 0.58
                 and math.dist((oldest[1], oldest[2]), pos[:2]) < 1.3)
        jump = False
        if stuck and me['grounded'] and me['energy'] > 34 and now >= self.recover_until:
            self.recover_until = now + 1.55
            self.route_until = 0.0
            jump = True
        if now < self.recover_until and (jump or not me['grounded']):
            running = me['energy'] > 14
            if move == (0.0, 0.0):
                move = desired
        elif not me['grounded']:
            running = False
        self.boosting, self.last_move = running, move
        action = {'tick': obs['tick'], 'move': list(move), 'boost': bool(running and (move != (0.0, 0.0) or jump)),
                  'jump': jump, 'fire': [False, False]}
        self.loot_action(obs, action)
        if target is not None:
            self.fire_control(obs, action, target, slot, solution)
        else:
            self.target_id = None
            if enemies and (retreating or nearest < 85):
                threat = enemies[0]['pos']
                yaw = math.atan2(threat[1] - pos[1], threat[0] - pos[0])
            elif self.damage and self.damage[-1][2] is not None:
                yaw = self.damage[-1][2]
            elif retreating or hold:
                yaw = me['aim'][0]
            else:
                yaw = math.atan2(goal[1] - pos[1], goal[0] - pos[0]) + 0.62 * math.sin(now * 0.85)
            action['aim'] = [yaw, 0.0]
            action['reload'] = any(w and w['kind'] == 'gun' and 0 < w.get('ammo', 0) <= 6 and not w.get('reload')
                                   for w in me['weapons'])
        if 'drop' in action:
            action['fire'][action['drop']] = False
        if obs['tick'] % 20 == 0:
            mf.draw.label(f'{mode} | {me["hp"]:.0f} HP | {me["cubes"]} cubes')
        return action


if __name__ == '__main__':
    mf.run(Bot)

"""Browser-UI message composition and send workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time
from typing import Any, Literal
from urllib.parse import ParseResult, parse_qs, urljoin, urlparse

import anyio
import anyio.lowlevel
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
import linkedin_mcp_server.scraping.contracts as contracts
from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

_MESSAGING_COMPOSE_SELECTOR = '[role="textbox"][contenteditable="true"]'

_PROFILE_MESSAGE_TARGET_JS = r"""() => {
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const active = anchor =>
        visible(anchor) &&
        !anchor.hasAttribute('disabled') &&
        (anchor.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const validComposeHref = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return false;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                url.pathname !== '/messaging/compose/'
            ) {
                return false;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            const normalized = values.map(item => {
                const text = item.trim();
                const prefix = 'urn:li:fsd_profile:';
                const identifier = text.startsWith(prefix)
                    ? text.slice(prefix.length)
                    : text;
                return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
            });
            return normalized.length > 0 &&
                normalized.every(item => item !== null && item === normalized[0]);
        } catch {
            return false;
        }
    };
    const main = document.querySelector('main');
    if (!main) return {status: 'unresolved'};

    const section = Array.from(main.children).find(
        element => element.matches('section') && visible(element)
    );
    if (!section) return {status: 'unresolved'};
    const headings = Array.from(section.querySelectorAll('h1')).filter(
        heading => visible(heading) && heading.closest('section') === section
    );
    const visibleComposeAnchors = Array.from(
        section.querySelectorAll('a[href*="/messaging/compose/"]')
    ).filter(anchor => visible(anchor) && anchor.closest('section') === section);
    const composeAnchors = visibleComposeAnchors.filter(active);
    if (
        headings.length !== 1 ||
        composeAnchors.length > 1 ||
        (composeAnchors.length === 1 && visibleComposeAnchors.length !== 1)
    ) {
        return {status: 'unresolved'};
    }
    if (composeAnchors.length === 0) {
        return visibleComposeAnchors.length === 0
            ? {status: 'unavailable', pageUrl: window.location.href}
            : {status: 'unresolved'};
    }

    const anchor = composeAnchors[0];
    const composeHref = anchor.getAttribute('href') || anchor.href || '';
    if (!validComposeHref(composeHref)) return {status: 'unresolved'};
    return {
        status: 'resolved',
        pageUrl: window.location.href,
        displayName: normalize(
            headings[0].innerText || headings[0].textContent || ''
        ),
        composeHrefs: [composeHref],
    };
}"""

_PROFILE_MESSAGE_TARGET_READY_JS = (
    f"() => ({_PROFILE_MESSAGE_TARGET_JS})().status === 'resolved'"
)
_PROFILE_MESSAGE_TARGET_TIMEOUT_MS = 1_000
_MESSAGE_SUBMIT_READY_TIMEOUT_MS = 1_000
_MESSAGE_CLEANUP_TIMEOUT_SECONDS = 1.0

_OWN_NAME_JS = r"""() => {
    const heading = document.querySelector('main h1');
    return ((heading && (heading.innerText || heading.textContent)) || '')
        .replace(/\s+/g, ' ').trim();
}"""

_JOB_POSTER_TARGET_JS = r"""() => {
    // The hiring-team card on a job page: one compose link whose recipient
    // params name one profile, next to that profile's own link. Anything
    // else (no card, two posters, a link elsewhere on the page) is unresolved.
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const recipientOf = href => {
        try {
            const url = new URL(href, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username || url.password || (url.port && url.port !== '443') ||
                url.hash || url.pathname !== '/messaging/compose/'
            ) {
                return null;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ].map(item => {
                const text = item.trim();
                const prefix = 'urn:li:fsd_profile:';
                const id = text.startsWith(prefix) ? text.slice(prefix.length) : text;
                return /^[A-Za-z0-9_-]+$/.test(id) ? id : null;
            });
            return values.length > 0 && values.every(v => v !== null && v === values[0])
                ? values[0]
                : null;
        } catch {
            return null;
        }
    };
    const main = document.querySelector('main');
    if (!main) return {status: 'unresolved'};
    const anchors = Array.from(main.querySelectorAll('a[href*="/messaging/compose/"]'))
        .filter(visible);
    if (anchors.length === 0) return {status: 'unavailable', pageUrl: window.location.href};
    if (anchors.length !== 1) return {status: 'unresolved', count: anchors.length};
    const anchor = anchors[0];
    const href = anchor.getAttribute('href') || anchor.href || '';
    const urn = recipientOf(href);
    if (!urn) return {status: 'unresolved'};
    // The poster's profile link: the closest ancestor of the compose link that
    // also holds exactly one /in/ link.
    let scope = anchor.parentElement;
    let profileLinks = [];
    for (let depth = 0; scope && scope !== main && depth < 8; depth += 1) {
        profileLinks = Array.from(scope.querySelectorAll('a[href*="/in/"]'));
        if (profileLinks.length > 0) break;
        scope = scope.parentElement;
    }
    const paths = new Set();
    let name = '';
    for (const link of profileLinks) {
        try {
            const url = new URL(link.getAttribute('href') || link.href || '', window.location.href);
            const match = /^\/in\/([^/?#]+)(?:\/.*)?$/.exec(url.pathname);
            if (!match) continue;
            paths.add(`/in/${match[1]}/`);
            const first = normalize((link.innerText || '').split('\n')[0]);
            if (first && (!name || first.length < name.length)) name = first;
        } catch {}
    }
    if (paths.size !== 1) return {status: 'unresolved', profiles: Array.from(paths)};
    return {
        status: 'resolved',
        pageUrl: window.location.href,
        composeHref: href,
        profileUrn: urn,
        profilePath: Array.from(paths)[0],
        displayName: name || null,
    };
}"""
_JOB_POSTER_TARGET_READY_JS = (
    "() => {"
    + 'const main = document.querySelector("main");'
    + 'return !!main && main.querySelectorAll(\'a[href*="/messaging/compose/"], a[href*="/in/"]\').length > 0;'
    + "}"
)
_JOB_POSTER_TARGET_TIMEOUT_MS = 10_000

_THREAD_PARTICIPANT_READY_JS = r"""() => {
    // The messaging page is an application shell: the thread, and with it the
    // participant links, render a moment after the route is reached.
    const root = document.querySelector('main') || document.body;
    return Array.from(root.querySelectorAll('a[href*="/in/"]'))
        .some(anchor => !anchor.closest('form'));
}"""
_THREAD_PARTICIPANT_TIMEOUT_MS = 10_000

_THREAD_PARTICIPANT_JS = r"""(arg) => {
    // Who the open conversation is with. Two sources, in order:
    //  1. a profile linked from the page outside the message history and the
    //     composer (the thread header of a regular conversation);
    //  2. the sender links inside the history, minus the viewer's own, since
    //     an InMail thread links nothing in its header.
    // Several links to one profile are fine; two different profiles are not.
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const ownName = normalize(arg.ownName).toLowerCase();
    const ownFirst = ownName.split(' ')[0] || '';
    const isOwn = text => {
        // A sender link names its profile by first name ("View Nicolas' profile")
        // or in full. A namesake gets dropped too, which then fails closed as
        // "no profile is linked" rather than picking the wrong one.
        const words = normalize(text).toLowerCase().split(/[^\p{L}\p{N}]+/u);
        return !!ownName && (
            normalize(text).toLowerCase().includes(ownName) || words.includes(ownFirst)
        );
    };
    // Identity links are read, not clicked, so hidden ones count too: the
    // sender link on a message is an accessibility link with no box of its own.
    const root = document.querySelector('main') || document.body;
    const groups = {header: new Map(), history: new Map()};
    let dropped = 0;
    let anchors = 0;
    for (const anchor of root.querySelectorAll('a[href*="/in/"]')) {
        anchors += 1;
        if (anchor.closest('form')) continue;
        let url;
        try {
            url = new URL(anchor.getAttribute('href') || anchor.href || '', window.location.href);
        } catch {
            continue;
        }
        const match = /^\/in\/([^/?#]+)(?:\/.*)?$/.exec(url.pathname);
        if (!match) continue;
        const path = `/in/${match[1]}/`;
        // A header link stacks the headline under the name; the name is line one.
        const text = normalize(
            (anchor.innerText || '').split('\n')[0] || anchor.getAttribute('aria-label') || ''
        );
        const inHistory = !!anchor.closest('[data-view-name="message-list-item"]');
        if (isOwn(text)) { dropped += 1; continue; }
        const group = inHistory ? groups.history : groups.header;
        const current = group.get(path);
        // The shortest text is the bare name; longer ones append a headline.
        if (current === undefined || (text && (!current || text.length < current.length))) {
            group.set(path, text);
        }
    }
    const describe = map => Array.from(map, ([path, name]) => ({path, name: name || null}));
    const header = describe(groups.header);
    const history = describe(groups.history);
    const found = header.length > 0 ? header : history;
    const source = header.length > 0 ? 'header' : 'history';
    if (found.length !== 1) {
        return {
            status: found.length === 0 ? 'none' : 'ambiguous',
            found, source, dropped, anchors,
        };
    }
    return {
        status: 'resolved', path: found[0].path, name: found[0].name,
        found, source, dropped, anchors,
    };
}"""

_MESSAGE_COMPOSER_INSPECT_JS = r"""
    // Text is compared after collapsing whitespace: a message with line breaks
    // renders as separate blocks in the editor and in the sent bubble, so the
    // exact innerText is not stable even though the words are.
    const sameText = (left, right) =>
        (left || '').replace(/\s+/g, ' ').trim() ===
        (right || '').replace(/\s+/g, ' ').trim();
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const normalizeUrn = value => {
        const text = (value || '').trim();
        const prefix = 'urn:li:fsd_profile:';
        const identifier = text.startsWith(prefix) ? text.slice(prefix.length) : text;
        return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
    };
    const profilePath = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return null;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash
            ) {
                return null;
            }
            const match = /^\/in\/([^/?#]+)(?:\/.*)?$/.exec(url.pathname);
            return match ? `/in/${match[1]}/` : null;
        } catch {
            return null;
        }
    };
    const messageRoute = target => {
        try {
            const url = new URL(window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                !(
                    url.pathname === '/messaging/compose/' ||
                    /^\/messaging\/thread\/[A-Za-z0-9_=-]+\/$/.test(url.pathname)
                )
            ) {
                return null;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            return values.every(value =>
                target.profileUrn === null || normalizeUrn(value) === target.profileUrn
            )
                ? url.href
                : null;
        } catch {
            return null;
        }
    };
    const inspect = target => {
        const editors = Array.from(
            document.querySelectorAll('[role="textbox"][contenteditable="true"]')
        ).filter(visible);
        if (editors.length !== 1) return {status: 'ambiguous_editor'};
        const editor = editors[0];
        const semanticAncestors = element => {
            const scopes = [];
            let ancestor = element.parentElement;
            while (ancestor) {
                if (ancestor.matches('form, dialog, [role="dialog"], main')) {
                    scopes.push(ancestor);
                }
                ancestor = ancestor.parentElement;
            }
            return scopes;
        };
        const localScopes = semanticAncestors(editor);
        if (localScopes.length === 0) return {status: 'missing_owner'};

        // The owner scopes the send confirmation, so it must contain the message
        // history as well as the editor: the compose overlay's dialog does; on a
        // conversation page the history sits beside the composer form, inside
        // main. Never wider than the innermost scope that holds both.
        const owner = localScopes.find(scope =>
            scope.matches('dialog, [role="dialog"]')
        ) || localScopes.find(scope =>
            scope.querySelector('[data-view-name="message-list-item"]')
        ) || localScopes[0];
        const outsideDraftAndHistory = element =>
            element !== editor &&
            !editor.contains(element) &&
            !element.closest('[data-view-name="message-list-item"]');
        const identityElements = selector => Array.from(new Set(
            localScopes.flatMap(scope => [
                ...(scope.matches(selector) ? [scope] : []),
                ...scope.querySelectorAll(selector),
            ])
        ));
        const paths = identityElements('a[href*="/in/"]')
            .filter(element => visible(element) && outsideDraftAndHistory(element))
            .map(anchor => profilePath(anchor.getAttribute('href') || anchor.href || ''));
        const urns = identityElements(
            '[data-profile-urn], [data-recipient-urn]'
        ).filter(
            element => visible(element) && outsideDraftAndHistory(element)
        ).flatMap(element =>
            ['data-profile-urn', 'data-recipient-urn']
                .filter(name => element.hasAttribute(name))
                .map(name => normalizeUrn(element.getAttribute(name)))
        );
        if (
            paths.some(path => path !== target.profilePath) ||
            urns.some(urn => target.profileUrn !== null && urn !== target.profileUrn)
        ) {
            return {status: 'recipient_mismatch'};
        }

        const submitButtons = scope => Array.from(
            scope.querySelectorAll(
                'button[type="submit"], button[data-control-name="send"]'
            )
        ).filter(button =>
            visible(button) &&
            !button.closest('[data-view-name="message-list-item"]')
        );
        const localScope = localScopes.find(scope => submitButtons(scope).length > 0)
            || localScopes[0];
        const buttons = submitButtons(localScope);
        return {
            status: 'valid',
            editor,
            ancestorChain: localScopes,
            localScope,
            owner,
            buttons,
            active: document.activeElement === editor,
            empty: !(editor.innerText || '').replace(/\s+/g, ' ').trim(),
            messageRoute: messageRoute(target),
        };
    };
"""

_MESSAGE_COMPOSER_OWNER_JS = (
    "(arg) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        const target = arg.target;
        const state = inspect(target);
        if (
            state.status !== 'valid' ||
            state.messageRoute !== arg.expectedRoute ||
            !state.owner.isConnected ||
            !state.editor.isConnected ||
            !state.owner.contains(state.editor) ||
            state.buttons.length !== 1
        ) {
            return null;
        }
        const button = state.buttons[0];
        if (
            !button.isConnected ||
            !state.localScope.contains(button) ||
            (button.form !== null && !state.ancestorChain.includes(button.form))
        ) {
            return null;
        }
        state.owner.__linkedinMcpComposer = {
            editor: state.editor,
            ancestorChain: state.ancestorChain,
            button,
            localScope: state.localScope,
            profilePath: target.profilePath,
            profileUrn: target.profileUrn,
            route: arg.expectedRoute,
            ownedMessage: null,
        };
        return state.owner;
    }"""
)

_MESSAGE_CONFIRMATION_PREPARE_JS = (
    "(arg) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + r"""
        const composer = inspect(arg);
        const pinned = arg.owner?.__linkedinMcpComposer;
        if (
            composer.status !== 'valid' ||
            composer.messageRoute !== pinned?.route ||
            !pinned ||
            composer.owner !== arg.owner ||
            composer.editor !== pinned.editor ||
            composer.ancestorChain.length !== pinned.ancestorChain.length ||
            composer.ancestorChain.some(
                (scope, index) => scope !== pinned.ancestorChain[index]
            ) ||
            composer.localScope !== pinned.localScope ||
            composer.buttons.length !== 1 ||
            composer.buttons[0] !== pinned.button ||
            pinned.button.disabled ||
            (pinned.button.getAttribute('aria-disabled') || '').toLowerCase()
                === 'true' ||
            !arg.owner.isConnected ||
            !pinned.editor.isConnected ||
            !arg.owner.contains(pinned.editor) ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.expected ||
            !sameText(pinned.editor.innerText || pinned.editor.textContent, arg.expected)
        ) {
            return null;
        }

        const counter = (arg.owner.__linkedinMcpConfirmationCounter || 0) + 1;
        arg.owner.__linkedinMcpConfirmationCounter = counter;
        const token = String(counter);
        const marker = document.createElement('span');
        marker.hidden = true;
        marker.setAttribute('data-linkedin-mcp-confirmation', token);
        marker.setAttribute('data-linkedin-mcp-invalid', 'false');
        arg.owner.appendChild(marker);
        pinned.editor.setAttribute('data-linkedin-mcp-editor', token);
        const state = {
            owner: arg.owner,
            editor: pinned.editor,
            expected: arg.expected,
            baseline: new Set(),
            candidates: new Map(),
            invalid: false,
        };
        const exactUnit = (node, requireVisible) => {
            if (requireVisible && !visible(node)) return false;
            const elements = [node, ...node.querySelectorAll('*')].filter(
                element => !requireVisible || visible(element)
            );
            const matches = elements.filter(
                element => sameText(element.innerText, state.expected)
            );
            const smallest = matches.filter(
                element => !matches.some(
                    other => other !== element && element.contains(other)
                )
            );
            return smallest.length === 1;
        };
        const remember = node => {
            if (!(node instanceof Element)) return;
            const items = [
                ...(node.matches('[data-view-name="message-list-item"]')
                    ? [node]
                    : []),
                ...node.querySelectorAll('[data-view-name="message-list-item"]'),
            ];
            for (const item of items) {
                if (state.baseline.has(item)) continue;
                if (!state.candidates.has(item)) {
                    item.setAttribute('data-linkedin-mcp-candidate', token);
                    state.candidates.set(item, {
                        transitioned: false,
                        matched: false,
                    });
                }
            }
        };
        const refresh = () => {
            for (const [node, candidate] of state.candidates) {
                if (
                    node.isConnected &&
                    state.owner.contains(node) &&
                    exactUnit(node, true)
                ) {
                    candidate.matched = true;
                    node.setAttribute('data-linkedin-mcp-matched', token);
                }
                if (candidate.matched && !node.isConnected) {
                    state.invalid = true;
                    marker.setAttribute('data-linkedin-mcp-invalid', 'true');
                }
            }
            if (
                Array.from(state.candidates.values()).filter(
                    candidate => candidate.matched
                ).length > 1
            ) {
                state.invalid = true;
                marker.setAttribute('data-linkedin-mcp-invalid', 'true');
            }
        };
        state.observer = new MutationObserver(records => {
            for (const record of records) {
                if (record.type !== 'childList') continue;
                for (const node of record.addedNodes) remember(node);
                for (const removed of record.removedNodes) {
                    if (!(removed instanceof Element)) continue;
                    if (removed === state.editor || removed.contains(state.editor)) {
                        state.invalid = true;
                        marker.setAttribute('data-linkedin-mcp-invalid', 'true');
                    }
                    for (const [candidate] of state.candidates) {
                        if (
                            (removed === candidate || removed.contains(candidate)) &&
                            exactUnit(candidate, false)
                        ) {
                            state.invalid = true;
                            marker.setAttribute(
                                'data-linkedin-mcp-invalid', 'true'
                            );
                        }
                    }
                }
            }
            for (const record of records) {
                if (
                    record.type !== 'attributes' ||
                    !state.candidates.has(record.target)
                ) {
                    continue;
                }
                const before = (record.oldValue || '').trim();
                const after = (
                    record.target.getAttribute('data-event-urn') || ''
                ).trim();
                if (before && after && before !== after) {
                    state.candidates.get(record.target).transitioned = true;
                    record.target.setAttribute(
                        'data-linkedin-mcp-transitioned', token
                    );
                }
            }
            refresh();
        });
        state.baseline = new Set(
            document.querySelectorAll('[data-view-name="message-list-item"]')
        );
        state.observer.observe(state.owner, {
            attributes: true,
            attributeFilter: ['data-event-urn'],
            attributeOldValue: true,
            childList: true,
            subtree: true,
        });
        if (!arg.owner.__linkedinMcpConfirmations) {
            arg.owner.__linkedinMcpConfirmations = new Map();
        }
        arg.owner.__linkedinMcpConfirmations.set(token, state);
        return token;
    }"""
)

_MESSAGE_CONFIRMATION_READY_JS = (
    "(arg) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + r"""
        if (!arg.owner?.isConnected) return false;
        const markers = Array.from(
            arg.owner.querySelectorAll('[data-linkedin-mcp-confirmation]')
        ).filter(
            marker => marker.getAttribute('data-linkedin-mcp-confirmation') === arg.token
        );
        if (
            markers.length !== 1 ||
            markers[0].getAttribute('data-linkedin-mcp-invalid') !== 'false'
        ) {
            return false;
        }
        const composer = inspect(arg);
        if (
            composer.status !== 'valid' ||
            composer.messageRoute === null ||
            composer.owner !== arg.owner ||
            composer.buttons.length !== 1 ||
            composer.editor.getAttribute('data-linkedin-mcp-editor') !== arg.token
        ) {
            return false;
        }
        const exactVisibleUnit = node => {
            if (!visible(node)) return false;
            const elements = [node, ...node.querySelectorAll('*')].filter(visible);
            const matches = elements.filter(
                element => sameText(element.innerText, arg.expected)
            );
            return matches.filter(
                element => !matches.some(
                    other => other !== element && element.contains(other)
                )
            ).length === 1;
        };
        const candidates = Array.from(
            arg.owner.querySelectorAll('[data-linkedin-mcp-candidate]')
        ).filter(node =>
            node.getAttribute('data-linkedin-mcp-candidate') === arg.token &&
            node.getAttribute('data-linkedin-mcp-matched') === arg.token &&
            node.getAttribute('data-linkedin-mcp-transitioned') === arg.token &&
            (node.getAttribute('data-event-urn') || '').trim() &&
            exactVisibleUnit(node)
        );
        return candidates.length === 1;
    }"""
)

_MESSAGE_CONFIRMATION_DISPOSE_JS = r"""arg => {
    const confirmations = arg.owner?.__linkedinMcpConfirmations;
    const state = confirmations?.get(arg.token);
    if (state?.observer) state.observer.disconnect();
    confirmations?.delete(arg.token);
    for (const element of arg.owner?.querySelectorAll(
        '[data-linkedin-mcp-candidate], [data-linkedin-mcp-editor], '
        + '[data-linkedin-mcp-confirmation]'
    ) || []) {
        for (const attribute of [
            'data-linkedin-mcp-candidate',
            'data-linkedin-mcp-matched',
            'data-linkedin-mcp-transitioned',
            'data-linkedin-mcp-editor',
        ]) {
            if (element.getAttribute(attribute) === arg.token) {
                element.removeAttribute(attribute);
            }
        }
        if (element.getAttribute('data-linkedin-mcp-confirmation') === arg.token) {
            element.remove();
        }
    }
}"""

_MESSAGE_COMPOSER_DISPOSE_JS = r"""owner => {
    const confirmations = owner?.__linkedinMcpConfirmations;
    for (const state of confirmations?.values() || []) {
        if (state?.observer) state.observer.disconnect();
    }
    confirmations?.clear();
    if (owner) {
        delete owner.__linkedinMcpConfirmations;
        delete owner.__linkedinMcpComposer;
    }
    for (const element of owner?.querySelectorAll(
        '[data-linkedin-mcp-candidate], [data-linkedin-mcp-editor], '
        + '[data-linkedin-mcp-confirmation]'
    ) || []) {
        element.removeAttribute('data-linkedin-mcp-candidate');
        element.removeAttribute('data-linkedin-mcp-matched');
        element.removeAttribute('data-linkedin-mcp-transitioned');
        element.removeAttribute('data-linkedin-mcp-editor');
        if (element.hasAttribute('data-linkedin-mcp-confirmation')) element.remove();
    }
}"""

_MESSAGE_COMPOSER_STATE_JS = (
    "(target) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        const state = inspect(target);
        return {
            status: state.status,
            active: state.active === true,
            empty: state.empty === true,
            submitCount: state.buttons ? state.buttons.length : 0,
            submitUsable: state.buttons?.length === 1 &&
                !state.buttons[0].disabled &&
                (state.buttons[0].getAttribute('aria-disabled') || '').toLowerCase()
                    !== 'true',
        };
    }"""
)

_MESSAGE_COMPOSER_READY_JS = (
    "(target) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        return inspect(target).status === 'valid';
    }"""
)

_MESSAGE_COMPOSER_FOCUS_JS = (
    "(target) => {"
    + _MESSAGE_COMPOSER_INSPECT_JS
    + """
        const state = inspect(target);
        if (state.status !== 'valid') return false;
        state.editor.focus();
        return state.editor.isConnected && document.activeElement === state.editor;
    }"""
)

_MESSAGE_COMPOSER_PINNED_JS = r"""
    // Text is compared after collapsing whitespace: a message with line breaks
    // renders as separate blocks in the editor and in the sent bubble, so the
    // exact innerText is not stable even though the words are.
    const sameText = (left, right) =>
        (left || '').replace(/\s+/g, ' ').trim() ===
        (right || '').replace(/\s+/g, ' ').trim();
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const normalizeUrn = value => {
        const text = (value || '').trim();
        const prefix = 'urn:li:fsd_profile:';
        const identifier = text.startsWith(prefix) ? text.slice(prefix.length) : text;
        return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
    };
    const profilePath = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return null;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash
            ) {
                return null;
            }
            const match = /^\/in\/([^/?#]+)(?:\/.*)?$/.exec(url.pathname);
            return match ? `/in/${match[1]}/` : null;
        } catch {
            return null;
        }
    };
    const messageRoute = target => {
        try {
            const url = new URL(window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                !(
                    url.pathname === '/messaging/compose/' ||
                    /^\/messaging\/thread\/[A-Za-z0-9_=-]+\/$/.test(url.pathname)
                )
            ) {
                return null;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            return values.every(value =>
                target.profileUrn === null || normalizeUrn(value) === target.profileUrn
            )
                ? url.href
                : null;
        } catch {
            return null;
        }
    };
    const semanticAncestors = element => {
        const scopes = [];
        let ancestor = element?.parentElement;
        while (ancestor) {
            if (ancestor.matches('form, dialog, [role="dialog"], main')) {
                scopes.push(ancestor);
            }
            ancestor = ancestor.parentElement;
        }
        return scopes;
    };
    const identitiesMatch = (scopes, editor, target) => {
        const outsideDraftAndHistory = element =>
            element !== editor &&
            !editor.contains(element) &&
            !element.closest('[data-view-name="message-list-item"]');
        const identityElements = selector => Array.from(new Set(
            scopes.flatMap(scope => [
                ...(scope.matches(selector) ? [scope] : []),
                ...scope.querySelectorAll(selector),
            ])
        ));
        const paths = identityElements('a[href*="/in/"]')
            .filter(element => visible(element) && outsideDraftAndHistory(element))
            .map(anchor => profilePath(anchor.getAttribute('href') || anchor.href || ''));
        const urns = identityElements(
            '[data-profile-urn], [data-recipient-urn]'
        ).filter(
            element => visible(element) && outsideDraftAndHistory(element)
        ).flatMap(element =>
            ['data-profile-urn', 'data-recipient-urn']
                .filter(name => element.hasAttribute(name))
                .map(name => normalizeUrn(element.getAttribute(name)))
        );
        return !(
            paths.some(path => path !== target.profilePath) ||
            urns.some(urn => target.profileUrn !== null && urn !== target.profileUrn)
        );
    };
    const validatePinned = (target, requireEnabled = true) => {
        const pinned = owner?.__linkedinMcpComposer;
        if (
            !pinned ||
            pinned.profilePath !== target.profilePath ||
            pinned.profileUrn !== target.profileUrn ||
            messageRoute(target) !== pinned.route
        ) {
            return null;
        }
        const {editor, ancestorChain, button, localScope} = pinned;
        const currentChain = semanticAncestors(editor);
        if (
            !owner.isConnected ||
            !editor?.isConnected ||
            !button?.isConnected ||
            !localScope?.isConnected ||
            !Array.isArray(ancestorChain) ||
            currentChain.length !== ancestorChain.length ||
            currentChain.some((scope, index) => scope !== ancestorChain[index]) ||
            !currentChain.includes(owner) ||
            !currentChain.includes(localScope) ||
            !owner.contains(editor) ||
            !owner.contains(localScope) ||
            !localScope.contains(button) ||
            (button.form !== null && !currentChain.includes(button.form)) ||
            !visible(editor) ||
            !visible(button) ||
            !editor.matches('[role="textbox"][contenteditable="true"]') ||
            !identitiesMatch(currentChain, editor, target)
        ) {
            return null;
        }
        const buttons = Array.from(localScope.querySelectorAll(
            'button[type="submit"], button[data-control-name="send"]'
        )).filter(candidate =>
            visible(candidate) &&
            !candidate.closest('[data-view-name="message-list-item"]')
        );
        if (
            buttons.length !== 1 ||
            buttons[0] !== button ||
            (requireEnabled && (
                button.disabled ||
                (button.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            ))
        ) {
            return null;
        }
        return pinned;
    };
"""

_MESSAGE_COMPOSER_WRITE_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + r"""
        let pinned = validatePinned(arg, false);
        if (!pinned) return 'invalid';
        const {editor} = pinned;
        if ((editor.innerText || '').replace(/\s+/g, ' ').trim()) {
            return 'occupied';
        }
        editor.focus();
        pinned = validatePinned(arg, false);
        if (!pinned || document.activeElement !== editor) return 'invalid';
        if ((editor.innerText || '').replace(/\s+/g, ' ').trim()) {
            return 'occupied';
        }
        if (
            typeof document.queryCommandSupported !== 'function' ||
            !document.queryCommandSupported('insertText') ||
            typeof document.execCommand !== 'function'
        ) {
            return 'unsupported';
        }
        // Line breaks are typed as paragraph breaks, what LinkedIn's composer
        // does on Shift+Enter, so that Enter-to-send never fires and the sent
        // message keeps its lines. execCommand emits input events like typing.
        const lines = arg.message.split('\n');
        let inserted = true;
        lines.forEach((line, index) => {
            if (index > 0 && inserted) {
                inserted = document.execCommand('insertParagraph', false) && inserted;
            }
            if (line && inserted) {
                inserted = document.execCommand('insertText', false, line) && inserted;
            }
        });
        if (sameText(editor.innerText || editor.textContent, arg.message)) {
            pinned.ownedMessage = arg.message;
        }
        if (inserted !== true) return 'unsupported';
        pinned = validatePinned(arg, false);
        if (
            !pinned ||
            document.activeElement !== editor ||
            pinned.ownedMessage !== arg.message ||
            !sameText(editor.innerText || editor.textContent, arg.message)
        ) {
            return 'invalid';
        }
        return 'written';
    }"""
)

_MESSAGE_COMPOSER_PREVIEW_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + r"""
        const pinned = validatePinned(arg, false);
        if (!pinned || pinned.ownedMessage !== arg.message) return null;
        return pinned.editor.innerText || pinned.editor.textContent || '';
    }"""
)

_MESSAGE_COMPOSER_SUBMIT_READY_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + r"""
        const pinned = validatePinned(arg, false);
        if (
            !pinned ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.message ||
            !sameText(pinned.editor.innerText || pinned.editor.textContent, arg.message)
        ) {
            return 'invalid';
        }
        return pinned.button.disabled ||
            (pinned.button.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            ? 'disabled'
            : 'ready';
    }"""
)

_MESSAGE_COMPOSER_CLEANUP_JS = r"""(owner, arg) => {
    // Text is compared after collapsing whitespace: a message with line breaks
    // renders as separate blocks in the editor and in the sent bubble, so the
    // exact innerText is not stable even though the words are.
    const sameText = (left, right) =>
        (left || '').replace(/\s+/g, ' ').trim() ===
        (right || '').replace(/\s+/g, ' ').trim();
    const pinned = owner?.__linkedinMcpComposer;
    if (!pinned || pinned.ownedMessage !== arg.message) return false;
    const {editor, ancestorChain} = pinned;
    const currentChain = [];
    let ancestor = editor?.parentElement;
    while (ancestor) {
        if (ancestor.matches('form, dialog, [role="dialog"], main')) {
            currentChain.push(ancestor);
        }
        ancestor = ancestor.parentElement;
    }
    if (
        !owner.isConnected ||
        !editor?.isConnected ||
        !Array.isArray(ancestorChain) ||
        currentChain.length !== ancestorChain.length ||
        currentChain.some((scope, index) => scope !== ancestorChain[index]) ||
        !currentChain.includes(owner) ||
        !owner.contains(editor) ||
        !sameText(editor.innerText || editor.textContent, arg.message)
    ) {
        return false;
    }
    pinned.ownedMessage = null;
    editor.replaceChildren();
    editor.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        composed: true,
        data: null,
        inputType: 'deleteContentBackward',
    }));
    return true;
}"""

_MESSAGE_COMPOSER_SUBMIT_JS = (
    "(owner, arg) => {"
    + _MESSAGE_COMPOSER_PINNED_JS
    + r"""
        const pinned = validatePinned(arg);
        if (
            !pinned ||
            document.activeElement !== pinned.editor ||
            pinned.ownedMessage !== arg.message ||
            !sameText(pinned.editor.innerText || pinned.editor.textContent, arg.message)
        ) {
            return 'invalid';
        }
        pinned.button.click();
        return 'clicked';
    }"""
)

_LINKEDIN_MESSAGE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")
_PROFILE_PATH_RE = re.compile(r"^/in/[^/?#]+/$")
# A thread id is base64url and keeps its padding literally. Measured live:
# /messaging/thread/2-ZDBkMjZiY2Ut...XzEwMA==/ is what LinkedIn redirects an
# existing conversation to, and rejecting it stopped every send to a member
# the account had already written to. Only '=' is added: '%' would readmit an
# encoded slash and let one path pose as another. The id identifies nobody on
# its own, and the recipient is proven by the composer rather than this path.
_MESSAGE_THREAD_PATH_RE = re.compile(r"^/messaging/thread/[A-Za-z0-9_=-]+/$")
_PROFILE_URN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PROFILE_URN_PREFIX = "urn:li:fsd_profile:"


@dataclass(frozen=True)
class _ProfileMessageTarget:
    profile_path: str
    profile_urn: (
        str | None
    )  # None for a thread reply: the route, not a URN, is the boundary
    compose_url: str
    display_name: str | None


@dataclass(frozen=True)
class _ProfileMessageTargetResolution:
    status: Literal["resolved", "unavailable", "failed"]
    target: _ProfileMessageTarget | None = None


def _safe_linkedin_url(value: str, *, base: str | None = None) -> ParseResult | None:
    """Parse an HTTPS LinkedIn URL without credentials or an ambiguous origin."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    candidate = urljoin(base, value.strip()) if base else value.strip()
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower().removesuffix(".")
    if (
        parsed.scheme != "https"
        or not _LINKEDIN_MESSAGE_HOST_RE.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return None
    return parsed


def _normalize_profile_urn(value: str | None) -> str | None:
    """Return the identifier carried by a profile URN or raw recipient value."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith(_PROFILE_URN_PREFIX):
        candidate = candidate[len(_PROFILE_URN_PREFIX) :]
    return candidate if _PROFILE_URN_RE.fullmatch(candidate) else None


def _profile_path_from_url(value: str) -> str | None:
    parsed = _safe_linkedin_url(value)
    if parsed is None or parsed.query or not _PROFILE_PATH_RE.fullmatch(parsed.path):
        return None
    try:
        username = normalize_person_identifier(value)
    except LinkedInScraperException:
        return None
    canonical_path = urlparse(person_profile_url(username, "/")).path
    return parsed.path if parsed.path == canonical_path else None


def _profile_urn_from_compose_url(value: str, *, base: str | None = None) -> str | None:
    parsed = _safe_linkedin_url(value, base=base)
    if parsed is None or parsed.path != "/messaging/compose/":
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    identifiers: set[str] = set()
    for key in ("recipient", "profileUrn"):
        values = params.get(key, [])
        normalized = [_normalize_profile_urn(item) for item in values]
        if any(item is None for item in normalized):
            return None
        identifiers.update(item for item in normalized if item is not None)
    if len(identifiers) != 1:
        return None
    return identifiers.pop()


def _message_page_url_is_safe(value: str, profile_urn: str | None) -> bool:
    """Accept only a compose or thread route whose recipient params match the URN.

    A None URN belongs to a thread reply, which pins its exact route instead and
    never calls this; it is refused here rather than treated as "any recipient".
    """
    parsed = _safe_linkedin_url(value)
    if parsed is None or profile_urn is None:
        return False

    params = parse_qs(parsed.query, keep_blank_values=True)
    recipient_values = [
        item for key in ("recipient", "profileUrn") for item in params.get(key, [])
    ]
    if parsed.path != "/messaging/compose/" and not _MESSAGE_THREAD_PATH_RE.fullmatch(
        parsed.path
    ):
        return False
    return all(_normalize_profile_urn(item) == profile_urn for item in recipient_values)


class MessageSender:
    """Compose and send messages through LinkedIn's browser UI."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._page = session.page
        self._own_name: str | None = None

    async def _read_profile_message_target(self) -> _ProfileMessageTargetResolution:
        """Resolve one recipient-specific top-card compose action after settling."""
        try:
            await self._page.wait_for_function(
                _PROFILE_MESSAGE_TARGET_READY_JS,
                timeout=_PROFILE_MESSAGE_TARGET_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Could not wait for the profile Message action", exc_info=True)

        try:
            data = await self._page.evaluate(_PROFILE_MESSAGE_TARGET_JS)
        except Exception:
            logger.debug("Could not inspect the profile Message action", exc_info=True)
            return _ProfileMessageTargetResolution("failed")
        if not isinstance(data, dict):
            return _ProfileMessageTargetResolution("failed")
        if data.get("status") == "unavailable":
            page_url = data.get("pageUrl")
            if (
                not isinstance(page_url, str)
                or _profile_path_from_url(page_url) is None
            ):
                return _ProfileMessageTargetResolution("failed")
            return _ProfileMessageTargetResolution("unavailable")
        if data.get("status") != "resolved":
            return _ProfileMessageTargetResolution("failed")

        page_url = data.get("pageUrl")
        compose_hrefs = data.get("composeHrefs")
        if not isinstance(page_url, str) or not isinstance(compose_hrefs, list):
            return _ProfileMessageTargetResolution("failed")
        profile_path = _profile_path_from_url(page_url)
        if profile_path is None:
            return _ProfileMessageTargetResolution("failed")
        if len(compose_hrefs) != 1 or not isinstance(compose_hrefs[0], str):
            return _ProfileMessageTargetResolution("failed")

        parsed_compose = _safe_linkedin_url(compose_hrefs[0], base=page_url)
        if parsed_compose is None:
            return _ProfileMessageTargetResolution("failed")
        compose_url = parsed_compose.geturl()
        profile_urn = _profile_urn_from_compose_url(compose_url)
        if profile_urn is None:
            return _ProfileMessageTargetResolution("failed")

        display_name = data.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        else:
            display_name = display_name.strip()
        return _ProfileMessageTargetResolution(
            "resolved",
            _ProfileMessageTarget(
                profile_path=profile_path,
                profile_urn=profile_urn,
                compose_url=compose_url,
                display_name=display_name,
            ),
        )

    async def _resolve_message_compose_href(self) -> str | None:
        """Return an unambiguous recipient-specific top-card compose URL."""
        resolution = await self._read_profile_message_target()
        return resolution.target.compose_url if resolution.target else None

    async def _wait_for_message_surface(
        self, target: _ProfileMessageTarget
    ) -> Literal["composer"] | None:
        """Wait for one editor with no contradictory local recipient identity."""
        if await self._wait_for_message_composer(target):
            return "composer"
        return None

    async def _wait_for_message_composer(self, target: _ProfileMessageTarget) -> bool:
        """Wait for the complete verified LinkedIn composer state to settle."""
        try:
            await self._page.wait_for_function(
                _MESSAGE_COMPOSER_READY_JS,
                arg=self._message_target_argument(target),
            )
        except PlaywrightTimeoutError:
            return False
        except Exception:
            logger.debug("Could not wait for the message editor", exc_info=True)
            return False
        return True

    async def _resolve_message_compose_box(self) -> Any | None:
        """Resolve the editor only when exactly one visible candidate exists."""
        locator = self._page.locator(f"{_MESSAGING_COMPOSE_SELECTOR}:visible")
        try:
            if await locator.count() != 1:
                return None
        except Exception:
            logger.debug("Could not count message editor candidates", exc_info=True)
            return None
        return locator.first

    @staticmethod
    def _message_target_argument(
        target: _ProfileMessageTarget,
    ) -> dict[str, str | bool | None]:
        return {
            "profilePath": target.profile_path,
            "profileUrn": target.profile_urn,
        }

    async def _read_message_composer_state(
        self, target: _ProfileMessageTarget
    ) -> dict[str, Any]:
        """Inspect the unique editor and reject contradictory local identity."""
        state = await self._page.evaluate(
            _MESSAGE_COMPOSER_STATE_JS,
            self._message_target_argument(target),
        )
        return state if isinstance(state, dict) else {"status": "invalid"}

    async def _focus_verified_message_editor(
        self, target: _ProfileMessageTarget
    ) -> bool:
        """Focus the same editor after local contradiction checks."""
        focused = await self._page.evaluate(
            _MESSAGE_COMPOSER_FOCUS_JS,
            self._message_target_argument(target),
        )
        return focused is True

    async def _write_verified_message(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> str:
        """Insert text synchronously into the pinned local editor."""
        result = await owner.evaluate(
            _MESSAGE_COMPOSER_WRITE_JS,
            {**self._message_target_argument(target), "message": message},
        )
        return result if result in {"written", "occupied", "unsupported"} else "invalid"

    async def _wait_for_verified_submit(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> bool:
        """Wait briefly for the exact pinned submit button to become active."""
        deadline = time.monotonic() + _MESSAGE_SUBMIT_READY_TIMEOUT_MS / 1_000
        argument = {**self._message_target_argument(target), "message": message}
        while True:
            try:
                state = await owner.evaluate(
                    _MESSAGE_COMPOSER_SUBMIT_READY_JS, argument
                )
                if state == "ready":
                    return True
                if state != "disabled":
                    return False
            except Exception:
                logger.debug(
                    "Could not wait for the pinned submit button", exc_info=True
                )
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))

    async def _submit_verified_message(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> str:
        """Click the one active submit button pinned with the local editor."""
        result = await owner.evaluate(
            _MESSAGE_COMPOSER_SUBMIT_JS,
            {**self._message_target_argument(target), "message": message},
        )
        return "clicked" if result == "clicked" else "invalid"

    @staticmethod
    async def _cleanup_owned_message(message: str, owner: Any) -> None:
        """Best-effort removal of text proven to belong to this tool call."""
        with anyio.move_on_after(
            _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
        ) as scope:
            try:
                await owner.evaluate(_MESSAGE_COMPOSER_CLEANUP_JS, {"message": message})
            except Exception:
                logger.debug("Could not clear tool-owned message text", exc_info=True)
        if scope.cancel_called:
            logger.warning("Timed out clearing tool-owned message text")
        await anyio.lowlevel.checkpoint()

    async def _resolve_message_owner(
        self,
        target: _ProfileMessageTarget,
        *,
        expected_route: str,
    ) -> Any | None:
        """Hold the verified owner node across submission and confirmation."""
        owner = await self._page.evaluate_handle(
            _MESSAGE_COMPOSER_OWNER_JS,
            arg={
                "target": self._message_target_argument(target),
                "expectedRoute": expected_route,
            },
        )
        if owner.as_element() is None:
            await self._dispose_message_owner(owner)
            return None
        return owner

    @staticmethod
    async def _dispose_message_owner(owner: Any) -> None:
        """Release all owner-scoped observers, pins, markers and handles."""
        try:
            with anyio.move_on_after(
                _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
            ) as dom_scope:
                try:
                    await owner.evaluate(_MESSAGE_COMPOSER_DISPOSE_JS)
                except Exception:
                    logger.debug("Could not clear pinned message nodes", exc_info=True)
            if dom_scope.cancel_called:
                logger.warning("Timed out clearing pinned message nodes")
        finally:
            with anyio.move_on_after(
                _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
            ) as handle_scope:
                try:
                    await owner.dispose()
                except Exception:
                    logger.debug(
                        "Could not release message owner handle", exc_info=True
                    )
            if handle_scope.cancel_called:
                logger.warning("Timed out releasing message owner handle")
        await anyio.lowlevel.checkpoint()

    def _message_confirmation_argument(
        self,
        message: str,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> dict[str, Any]:
        return {
            **self._message_target_argument(target),
            "expected": message,
            "owner": owner,
        }

    async def _prepare_message_confirmation(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
    ) -> str | None:
        """Start the owner-scoped DOM observer immediately before submission."""
        token = await self._page.evaluate(
            _MESSAGE_CONFIRMATION_PREPARE_JS,
            self._message_confirmation_argument(message, target, owner),
        )
        return token if isinstance(token, str) and token else None

    async def _message_send_confirmed(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        owner: Any,
        confirmation: str,
    ) -> bool:
        """Wait for one message-list node to gain a different opaque event ID.

        The observer accepts only a node inserted after it was installed whose
        exact visible message unit equals the typed text. That same connected
        node must then change from one non-empty ``data-event-urn`` value to a
        different non-empty value. Every timeout, remount, replacement or
        ambiguity answers "not observed" because submission already happened.
        """
        try:
            await self._page.wait_for_function(
                _MESSAGE_CONFIRMATION_READY_JS,
                arg={
                    **self._message_target_argument(target),
                    "expected": message,
                    "owner": owner,
                    "token": confirmation,
                },
            )
            return True
        except Exception:
            logger.debug("Message send could not be confirmed", exc_info=True)
            return False

    async def _dispose_message_confirmation(
        self, owner: Any, confirmation: str
    ) -> None:
        """Disconnect a request-local confirmation observer."""
        with anyio.move_on_after(
            _MESSAGE_CLEANUP_TIMEOUT_SECONDS, shield=True
        ) as scope:
            try:
                await self._page.evaluate(
                    _MESSAGE_CONFIRMATION_DISPOSE_JS,
                    {"owner": owner, "token": confirmation},
                )
            except Exception:
                logger.debug("Could not disconnect message observer", exc_info=True)
        if scope.cancel_called:
            logger.warning("Timed out disconnecting message observer")
        await anyio.lowlevel.checkpoint()

    async def send_message(
        self,
        linkedin_username: str,
        message: str,
        *,
        confirm_send: bool,
        profile_urn: str | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Compose and send a new message with explicit confirmation gating.

        Opens LinkedIn's profile-based compose flow. That may create a separate
        DM instead of replying in an existing recruiter/InMail or messaging
        thread. Recipient authorization comes from the validated top-card action
        carrying the target URN and the browser navigation it initiates. The exact
        resulting route is pinned through every later operation; visible local
        identities are optional corroboration, but any contradiction fails closed.

        Args:
            linkedin_username: LinkedIn username of the recipient.
            message: The message text to send.
            confirm_send: Must be True to actually send (False does a dry run).
            profile_urn: Optional profile URN (e.g. ACoAAB...) to verify against
                the recipient resolved from the loaded profile snapshot.
            preview: With confirm_send False, also type the message into the
                verified composer, report the editor text as ``preview`` and
                clear it again, so line handling can be checked before sending.
        """
        message = contracts.normalize_message_text(message)
        refusal = contracts.refuse_an_invalid_message(linkedin_username, message)
        if refusal is not None:
            return refusal
        linkedin_username = normalize_person_identifier(linkedin_username)
        profile_url = person_profile_url(linkedin_username, "/")

        await self._navigator._navigate_to_page(profile_url)
        await self._session.check_rate_limit()

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Profile page did not load for %s", linkedin_username)

        resolution = await self._read_profile_message_target()
        if resolution.status == "unavailable":
            return contracts.message_action_result(
                profile_url,
                "message_unavailable",
                "LinkedIn did not expose a normal Message action for this profile. "
                "Use connect_with_person first, then retry only after the connection "
                "request is accepted.",
            )
        target = resolution.target
        if target is None:
            return contracts.message_action_result(
                profile_url,
                "recipient_resolution_failed",
                "LinkedIn did not expose one unambiguous recipient-specific Message "
                "action.",
            )

        supplied_urn = _normalize_profile_urn(profile_urn) if profile_urn else None
        if profile_urn is not None and supplied_urn != target.profile_urn:
            return contracts.message_action_result(
                profile_url,
                "recipient_resolution_failed",
                "The supplied profile URN did not match the loaded profile.",
            )

        # The validated top-card action and its browser navigation are the
        # recipient boundary. LinkedIn may strip the query and expose no local
        # identity, so capture the final route now and fail on any later change or
        # visible contradiction. Do not replace this with a Voyager/private API.
        await self._navigator._navigate_to_page(target.compose_url)
        expected_route = self._page.url
        if not _message_page_url_is_safe(expected_route, target.profile_urn):
            return contracts.message_action_result(
                expected_route,
                "recipient_resolution_failed",
                "LinkedIn opened an unexpected messaging URL.",
            )
        return await self._send_in_composer(
            target,
            message,
            expected_route=expected_route,
            url_is_safe=lambda url: _message_page_url_is_safe(url, target.profile_urn),
            confirm_send=confirm_send,
            preview=preview,
            label=linkedin_username,
        )

    async def reply_in_thread(
        self,
        thread_id: str,
        message: str,
        *,
        confirm_send: bool,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Reply inside an existing messaging thread with confirmation gating.

        The thread route itself is the recipient boundary: the page must open at
        exactly ``/messaging/thread/<thread_id>/`` and stay there through every
        later step. The conversation's single visible participant profile is
        recorded on the result as ``recipient_profile_path`` so the caller can
        cross-check it; a thread that exposes no single participant (a group
        conversation, or markup without a profile link) fails closed.
        """
        message = contracts.normalize_message_text(message)
        refusal = contracts.refuse_an_invalid_thread_reply(thread_id, message)
        if refusal is not None:
            return refusal
        thread_url = contracts.message_thread_url(thread_id)

        own_name = await self._read_own_name()
        await self._navigator._navigate_to_page(thread_url)
        expected_route = self._page.url
        parsed = _safe_linkedin_url(expected_route)
        if parsed is None or parsed.path != urlparse(thread_url).path or parsed.query:
            return contracts.message_action_result(
                expected_route,
                "thread_unavailable",
                "LinkedIn did not open the requested conversation.",
            )

        participant, detail = await self._read_thread_participant(own_name)
        if participant is None:
            return contracts.message_action_result(
                expected_route,
                "recipient_resolution_failed",
                "The conversation did not expose exactly one participant profile "
                f"({detail}).",
            )
        target = _ProfileMessageTarget(
            profile_path=participant["path"],
            profile_urn=None,
            compose_url=expected_route,
            display_name=participant.get("name"),
        )
        result = await self._send_in_composer(
            target,
            message,
            expected_route=expected_route,
            url_is_safe=lambda url: url == expected_route,
            confirm_send=confirm_send,
            preview=preview,
            label=thread_id,
        )
        result["recipient_profile_path"] = target.profile_path
        if target.display_name:
            result["recipient_name"] = target.display_name
        return result

    async def message_job_poster(
        self,
        job_id: str,
        message: str,
        *,
        confirm_send: bool,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Message the poster of a job through the listing's hiring-team card.

        The card's Message link is a recipient-specific compose URL, so the
        same verified compose flow as send_message applies from there on: the
        URN in that link is the recipient boundary and the compose route is
        pinned. This is the free route to a poster who is not a connection,
        where the profile page offers no plain Message action.
        """
        message = contracts.normalize_message_text(message)
        refusal = contracts.refuse_an_invalid_job_message(job_id, message)
        if refusal is not None:
            return refusal
        job_url = contracts.job_url(job_id)

        await self._navigator._navigate_to_page(job_url)
        await self._session.check_rate_limit()
        try:
            await self._page.wait_for_function(
                _JOB_POSTER_TARGET_READY_JS, timeout=_JOB_POSTER_TARGET_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Could not wait for the job page to render", exc_info=True)
        try:
            data = await self._page.evaluate(_JOB_POSTER_TARGET_JS)
        except Exception:
            logger.debug("Could not inspect the job's hiring team", exc_info=True)
            data = None
        if not isinstance(data, dict) or data.get("status") == "unresolved":
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The job page did not expose one unambiguous hiring-team Message "
                "link with one poster profile.",
            )
        if data.get("status") == "unavailable":
            return contracts.message_action_result(
                self._page.url,
                "message_unavailable",
                "The job page shows no hiring-team Message link.",
            )
        compose_href = data.get("composeHref")
        parsed_compose = (
            _safe_linkedin_url(compose_href, base=self._page.url)
            if isinstance(compose_href, str)
            else None
        )
        profile_urn = (
            _profile_urn_from_compose_url(parsed_compose.geturl())
            if parsed_compose is not None
            else None
        )
        profile_path = data.get("profilePath")
        if (
            parsed_compose is None
            or profile_urn is None
            or profile_urn != data.get("profileUrn")
            or not isinstance(profile_path, str)
            or not _PROFILE_PATH_RE.fullmatch(profile_path)
        ):
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The hiring-team Message link did not name one recipient.",
            )
        display_name = data.get("displayName")
        target = _ProfileMessageTarget(
            profile_path=profile_path,
            profile_urn=profile_urn,
            compose_url=parsed_compose.geturl(),
            display_name=display_name if isinstance(display_name, str) else None,
        )

        await self._navigator._navigate_to_page(target.compose_url)
        expected_route = self._page.url
        if not _message_page_url_is_safe(expected_route, target.profile_urn):
            return contracts.message_action_result(
                expected_route,
                "recipient_resolution_failed",
                "LinkedIn opened an unexpected messaging URL.",
            )
        result = await self._send_in_composer(
            target,
            message,
            expected_route=expected_route,
            url_is_safe=lambda url: _message_page_url_is_safe(url, target.profile_urn),
            confirm_send=confirm_send,
            preview=preview,
            label=f"job {job_id}",
        )
        result["recipient_profile_path"] = target.profile_path
        if target.display_name:
            result["recipient_name"] = target.display_name
        return result

    async def _read_own_name(self) -> str:
        """Return the viewer's display name, resolved once per session via /in/me/.

        Sender links in a conversation carry the viewer's name, and the name is
        the one locale-independent way to tell those links from the other
        participant's. Unknown (no heading) resolves to "", which drops nothing.
        """
        if self._own_name is None:
            try:
                await self._navigator._navigate_to_page(
                    "https://www.linkedin.com/in/me/"
                )
                name = await self._page.evaluate(_OWN_NAME_JS)
            except Exception:
                logger.debug("Could not resolve the viewer's own name", exc_info=True)
                name = ""
            self._own_name = name if isinstance(name, str) else ""
        return self._own_name

    async def _read_thread_participant(
        self, own_name: str
    ) -> tuple[dict[str, str] | None, str]:
        """Return the one profile the open conversation is with, if unambiguous.

        The second element says what the page exposed, for the failure message:
        which profiles were linked, or that none were.
        """
        try:
            await self._page.wait_for_function(
                _THREAD_PARTICIPANT_READY_JS,
                timeout=_THREAD_PARTICIPANT_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Could not wait for the conversation to render", exc_info=True)
        try:
            data = await self._page.evaluate(
                _THREAD_PARTICIPANT_JS, {"ownName": own_name}
            )
        except Exception:
            logger.debug(
                "Could not inspect the conversation participant", exc_info=True
            )
            return None, "the participant could not be inspected"
        if not isinstance(data, dict):
            return None, "the participant could not be inspected"
        found = data.get("found")
        listing = ", ".join(
            f"{item.get('name') or '?'} ({item.get('path')})"
            for item in (found if isinstance(found, list) else [])
            if isinstance(item, dict)
        )
        detail = (
            f"profiles linked in the {data.get('source')}: {listing}"
            if listing
            else f"no profile is linked ({data.get('anchors', 0)} profile anchors on the page)"
        )
        if data.get("dropped"):
            detail += f"; {data['dropped']} link(s) to the viewer's own profile skipped"
        path = data.get("path")
        if not isinstance(path, str) or not _PROFILE_PATH_RE.fullmatch(path):
            return None, detail
        name = data.get("name")
        return {
            "path": path,
            **({"name": name} if isinstance(name, str) and name.strip() else {}),
        }, detail

    async def _send_in_composer(
        self,
        target: _ProfileMessageTarget,
        message: str,
        *,
        expected_route: str,
        url_is_safe: Any,
        confirm_send: bool,
        preview: bool,
        label: str,
    ) -> dict[str, Any]:
        """Verify the pinned composer on the current page, then type and submit.

        Shared by the profile compose flow and the thread reply flow once each
        has navigated to its route and pinned ``expected_route``.
        """
        await self._session.check_rate_limit()
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed while the composer was loading.",
            )

        try:
            await self._page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Compose page did not fully load for %s", label)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed while the composer was loading.",
            )

        message_surface = await self._wait_for_message_surface(target)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed while the composer was loading.",
            )
        logger.debug("Message surface for %s was %s", label, message_surface)
        if message_surface != "composer":
            return contracts.message_action_result(
                self._page.url,
                "composer_unavailable",
                "LinkedIn did not expose one usable message composer.",
            )

        state = await self._read_message_composer_state(target)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed during recipient verification.",
            )
        if state.get("status") != "valid":
            logger.debug(
                "Message recipient verification for %s returned %s",
                label,
                state.get("status"),
            )
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The local composer did not identify exactly the requested profile.",
            )
        recipient_selected = True

        if not confirm_send:
            result = contracts.message_action_result(
                self._page.url,
                "confirmation_required",
                "Set confirm_send=true to send the message.",
                recipient_selected=recipient_selected,
            )
            if preview:
                result["preview"] = await self._preview_message(
                    message, target=target, expected_route=expected_route
                )
            return result

        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed before text entry.",
                recipient_selected=recipient_selected,
            )
        state = await self._read_message_composer_state(target)
        if self._page.url != expected_route:
            return contracts.message_action_result(
                self._page.url,
                "recipient_resolution_failed",
                "The messaging URL changed before text entry.",
                recipient_selected=recipient_selected,
            )
        if state.get("status") == "valid" and state.get("empty") is not True:
            # Text already in the editor belongs to whoever typed it. Clearing
            # it would trade a recipient leak for destroying their draft.
            return contracts.message_action_result(
                self._page.url,
                "composer_occupied",
                "The composer already holds a draft that would be sent along "
                "with the message. The draft was left untouched.",
                recipient_selected=recipient_selected,
            )
        if state.get("status") != "valid":
            return contracts.message_action_result(
                self._page.url,
                "compose_interact_failed",
                "The verified message composer changed before text entry.",
                recipient_selected=recipient_selected,
            )
        if state.get("submitCount") != 1:
            return contracts.message_action_result(
                self._page.url,
                "send_unavailable",
                "The local submit path was missing or ambiguous.",
                recipient_selected=recipient_selected,
            )

        may_have_submitted = False
        try:
            owner = await self._resolve_message_owner(
                target, expected_route=expected_route
            )
            if owner is None:
                return contracts.message_action_result(
                    self._page.url,
                    "recipient_resolution_failed",
                    "The verified message composer changed before text entry.",
                    recipient_selected=recipient_selected,
                )

            try:
                write_result = await self._write_verified_message(
                    message,
                    target=target,
                    owner=owner,
                )
                if not url_is_safe(self._page.url):
                    return contracts.message_action_result(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The messaging URL changed during text entry.",
                        recipient_selected=recipient_selected,
                    )
                if write_result == "occupied":
                    return contracts.message_action_result(
                        self._page.url,
                        "composer_occupied",
                        "The composer already holds a draft that would be sent along "
                        "with the message. The draft was left untouched.",
                        recipient_selected=recipient_selected,
                    )
                if write_result != "written":
                    return contracts.message_action_result(
                        self._page.url,
                        "compose_interact_failed",
                        "The verified message editor could not accept the message.",
                        recipient_selected=recipient_selected,
                    )

                if not await self._wait_for_verified_submit(
                    message,
                    target=target,
                    owner=owner,
                ):
                    return contracts.message_action_result(
                        self._page.url,
                        "send_unavailable",
                        "The pinned submit button did not become available without "
                        "changing the verified composer.",
                        recipient_selected=recipient_selected,
                    )

                confirmation = await self._prepare_message_confirmation(
                    message,
                    target=target,
                    owner=owner,
                )
                if confirmation is None:
                    return contracts.message_action_result(
                        self._page.url,
                        "recipient_resolution_failed",
                        "The verified message composer changed before submission.",
                        recipient_selected=recipient_selected,
                    )

                try:
                    try:
                        # A click can dispatch before the evaluate call reports an
                        # error, so an exception from this round trip is ambiguous.
                        may_have_submitted = True
                        submission = await self._submit_verified_message(
                            message,
                            target=target,
                            owner=owner,
                        )
                    except Exception:
                        logger.debug(
                            "Message submission did not complete", exc_info=True
                        )
                        return contracts.message_action_result(
                            self._page.url,
                            "send_unconfirmed",
                            "The message submission was interrupted and LinkedIn did "
                            "not confirm the send. Check the conversation before "
                            "retrying; retrying may deliver the message twice.",
                            recipient_selected=recipient_selected,
                            retry_safe=False,
                        )

                    if submission != "clicked":
                        may_have_submitted = False
                        return contracts.message_action_result(
                            self._page.url,
                            "send_unavailable",
                            "The local submit path was missing, disabled, or ambiguous.",
                            recipient_selected=recipient_selected,
                        )

                    confirmed = await self._message_send_confirmed(
                        message,
                        target=target,
                        owner=owner,
                        confirmation=confirmation,
                    )
                    if not confirmed:
                        return contracts.message_action_result(
                            self._page.url,
                            "send_unconfirmed",
                            "The message was submitted but LinkedIn did not confirm "
                            "the message-list transition in time. Check the "
                            "conversation before retrying; retrying may deliver the "
                            "message twice.",
                            recipient_selected=recipient_selected,
                            retry_safe=False,
                        )

                    return contracts.message_action_result(
                        self._page.url,
                        "sent",
                        "Message submitted and confirmed in the conversation UI.",
                        recipient_selected=recipient_selected,
                        sent=True,
                        retry_safe=False,
                    )
                finally:
                    await self._dispose_message_confirmation(owner, confirmation)
            finally:
                try:
                    if not may_have_submitted:
                        await self._cleanup_owned_message(message, owner)
                finally:
                    await self._dispose_message_owner(owner)
        except Exception:
            if not may_have_submitted:
                # Nothing can have been submitted yet, so the error itself is
                # the useful answer and the caller can retry on it.
                raise
            logger.debug(
                "Message send failed after a possible submission", exc_info=True
            )
            return contracts.message_action_result(
                self._page.url,
                "send_unconfirmed",
                "The message may already have been submitted when the send "
                "failed, and LinkedIn did not confirm the outcome. Check the "
                "conversation before retrying; retrying may deliver the "
                "message twice.",
                recipient_selected=recipient_selected,
                retry_safe=False,
            )
        except BaseException:
            # Cancellation only. FastMCP runs the tool inside
            # `anyio.fail_after()` and a cancelled scope discards whatever it
            # returns, so the answer the branch above gives cannot be given
            # here and the log line is all that is left.
            #
            # Silent before explicit submission: nothing can have left yet,
            # and a warning about duplicate delivery would be false.
            if may_have_submitted:
                logger.warning(contracts.SEND_INTERRUPTED_WARNING)
            raise

    async def _preview_message(
        self,
        message: str,
        *,
        target: _ProfileMessageTarget,
        expected_route: str,
    ) -> str | None:
        """Type the message, read the editor text back, and clear it again.

        Nothing is submitted. None means the composer would not take the text
        (occupied, changed, or unsupported); the caller should not send blind.
        """
        owner = await self._resolve_message_owner(target, expected_route=expected_route)
        if owner is None:
            return None
        try:
            written = await self._write_verified_message(
                message, target=target, owner=owner
            )
            if written != "written":
                return None
            text = await owner.evaluate(
                _MESSAGE_COMPOSER_PREVIEW_JS,
                {**self._message_target_argument(target), "message": message},
            )
            return text if isinstance(text, str) else None
        finally:
            try:
                await self._cleanup_owned_message(message, owner)
            finally:
                await self._dispose_message_owner(owner)

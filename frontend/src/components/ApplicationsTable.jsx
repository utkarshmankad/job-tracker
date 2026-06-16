import { useState, useEffect, useRef, useMemo, startTransition } from "react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  flexRender,
} from "@tanstack/react-table";
import { Eye, ArrowUp, ArrowDown, ChevronsUpDown } from "lucide-react";
import { api } from "../api/client";
import { STATUS_COLORS } from "../utils/constants";
import { formatDate } from "../utils/formatters";

const BATCH = 30; // rows revealed per scroll trigger

function SortIcon({ column }) {
  if (!column.getCanSort()) return null;
  if (column.getIsSorted() === "asc")
    return <ArrowUp size={12} className="inline ml-1 text-blue-500" aria-hidden="true" />;
  if (column.getIsSorted() === "desc")
    return <ArrowDown size={12} className="inline ml-1 text-blue-500" aria-hidden="true" />;
  return <ChevronsUpDown size={12} className="inline ml-1 opacity-50" aria-hidden="true" />;
}

export default function ApplicationsTable({ filters, onSelectId }) {
  const [data, setData] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sorting, setSorting] = useState([]);
  const [displayCount, setDisplayCount] = useState(BATCH);
  const sentinelRef = useRef(null);

  // Fetch all data when filters change; reset scroll position.
  useEffect(() => {
    const params = {
      ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v !== "" && v != null)),
      page_size: 500,
    };
    setLoading(true);
    setDisplayCount(BATCH);
    api
      .listApplications(params)
      .then((res) => {
        setData(Array.isArray(res) ? res : res.items ?? []);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters]);

  // Reveal the next batch whenever the sentinel scrolls into view.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          startTransition(() => {
            setDisplayCount((c) => Math.min(c + BATCH, data.length));
          });
        }
      },
      { threshold: 0 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [data.length]);

  const columns = useMemo(
    () => [
      {
        accessorKey: "company",
        header: "Company",
        cell: (info) => info.getValue() || "—",
      },
      {
        accessorKey: "role",
        header: "Role",
        cell: (info) => info.getValue() || "—",
      },
      {
        accessorKey: "source_portal",
        header: "Source",
      },
      {
        accessorKey: "applied_date",
        header: "Applied Date",
        cell: (info) => formatDate(info.getValue()),
      },
      {
        accessorKey: "current_status",
        header: "Status",
        cell: (info) => {
          const status = info.getValue();
          const colorClass = STATUS_COLORS[status] || "bg-gray-100 text-gray-700";
          return (
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${colorClass}`}>
              {status}
            </span>
          );
        },
      },
      {
        accessorKey: "is_stale",
        header: "",
        enableSorting: false,
        cell: (info) =>
          info.getValue() ? (
            <span title="Stale — no update in 14+ days" className="text-amber-400 text-xs">⚠</span>
          ) : null,
      },
      {
        id: "actions",
        header: "",
        enableSorting: false,
        cell: (info) => (
          <button
            onClick={() => onSelectId(info.row.original.id)}
            aria-label="View application"
            title="View"
            className="p-1.5 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded-md transition-colors"
          >
            <Eye size={15} aria-hidden="true" />
          </button>
        ),
      },
    ],
    [onSelectId]
  );

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: (next) => {
      setSorting(next);
      setDisplayCount(BATCH); // reset scroll when sort changes
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const allRows = table.getRowModel().rows;
  const visibleRows = allRows.slice(0, displayCount);
  const hasMore = displayCount < allRows.length;

  if (loading)
    return <div className="text-sm text-gray-500 dark:text-gray-400 py-8 text-center">Loading…</div>;
  if (error)
    return <div className="text-sm text-red-600 py-8 text-center">Error: {error}</div>;

  return (
    <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((header) => (
                <th
                  key={header.id}
                  onClick={header.column.getToggleSortingHandler()}
                  className={`px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider ${
                    header.column.getCanSort()
                      ? "cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-200"
                      : ""
                  }`}
                >
                  {flexRender(header.column.columnDef.header, header.getContext())}
                  <SortIcon column={header.column} />
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
          {visibleRows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-4 py-8 text-center text-gray-400">
                No applications found.
              </td>
            </tr>
          ) : (
            visibleRows.map((row) => {
              const stale = row.original.is_stale;
              return (
                <tr
                  key={row.id}
                  className={`hover:bg-gray-50 dark:hover:bg-gray-800 ${
                    stale ? "opacity-50" : ""
                  }`}
                >
                  {row.getVisibleCells().map((cell, cellIdx) => (
                    <td
                      key={cell.id}
                      className={`px-4 py-3 text-gray-700 dark:text-gray-300 ${
                        cellIdx === 0 && stale ? "border-l-4 border-amber-400" : ""
                      }`}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              );
            })
          )}
        </tbody>
      </table>

      {/* Sentinel + footer */}
      <div
        ref={sentinelRef}
        className="px-4 py-3 border-t border-gray-200 dark:border-gray-700 text-xs text-gray-400 dark:text-gray-500 text-center"
      >
        {data.length === 0
          ? null
          : hasMore
          ? `Showing ${visibleRows.length} of ${data.length} — scroll for more`
          : `All ${data.length} application${data.length !== 1 ? "s" : ""} shown`}
      </div>
    </div>
  );
}

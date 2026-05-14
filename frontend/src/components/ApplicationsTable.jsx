import { useState, useEffect, useMemo } from "react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  flexRender,
} from "@tanstack/react-table";
import { api } from "../api/client";
import { STATUS_COLORS } from "../utils/constants";
import { formatDate } from "../utils/formatters";

const PAGE_SIZE = 20;

export default function ApplicationsTable({ filters, onSelectId }) {
  const [data, setData] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sorting, setSorting] = useState([]);

  useEffect(() => {
    const params = Object.fromEntries(
      Object.entries(filters).filter(([, v]) => v !== "" && v != null)
    );
    setLoading(true);
    api
      .listApplications(params)
      .then((res) => {
        setData(Array.isArray(res) ? res : res.items ?? []);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [filters]);

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
        header: "Stale",
        enableSorting: false,
        cell: (info) =>
          info.getValue() ? (
            <span title="Stale — no update in 14 days">⚠️</span>
          ) : null,
      },
      {
        id: "actions",
        header: "Actions",
        enableSorting: false,
        cell: (info) => (
          <button
            onClick={() => onSelectId(info.row.original.id)}
            className="px-2 py-1 text-xs bg-blue-50 text-blue-700 border border-blue-200 rounded hover:bg-blue-100"
          >
            View
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
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: PAGE_SIZE } },
  });

  if (loading) return <div className="text-sm text-gray-500 py-8 text-center">Loading…</div>;
  if (error) return <div className="text-sm text-red-600 py-8 text-center">Error: {error}</div>;

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((header) => (
                <th
                  key={header.id}
                  onClick={header.column.getToggleSortingHandler()}
                  className={`px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider ${
                    header.column.getCanSort() ? "cursor-pointer select-none" : ""
                  }`}
                >
                  {flexRender(header.column.columnDef.header, header.getContext())}
                  {header.column.getIsSorted() === "asc" && " ↑"}
                  {header.column.getIsSorted() === "desc" && " ↓"}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody className="divide-y divide-gray-100">
          {table.getRowModel().rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-4 py-8 text-center text-gray-400">
                No applications found.
              </td>
            </tr>
          ) : (
            table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className={`hover:bg-gray-50 ${
                  row.original.is_stale ? "border-l-4 border-amber-400" : ""
                }`}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-3 text-gray-700">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>

      <div className="flex items-center justify-between px-4 py-3 border-t border-gray-200 text-sm text-gray-600">
        <span>
          Page {table.getState().pagination.pageIndex + 1} of{" "}
          {Math.max(table.getPageCount(), 1)}
        </span>
        <div className="flex gap-2">
          <button
            onClick={() => table.previousPage()}
            disabled={!table.getCanPreviousPage()}
            className="px-3 py-1 border border-gray-300 rounded disabled:opacity-40 hover:bg-gray-50"
          >
            Previous
          </button>
          <button
            onClick={() => table.nextPage()}
            disabled={!table.getCanNextPage()}
            className="px-3 py-1 border border-gray-300 rounded disabled:opacity-40 hover:bg-gray-50"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
